from __future__ import annotations

"""
Talk-back API — commissioning a camera's speaker, and the live microphone.

Four HTTP endpoints and one WebSocket:

    GET    /api/v1/talkback/cameras                  what can be talked to
    PUT    /api/v1/talkback/{camera_id}/credential   set the TP-Link password
    DELETE /api/v1/talkback/{camera_id}/credential   forget it
    POST   /api/v1/talkback/{camera_id}/test         prove the chain, silently
    WS     /api/v1/talkback/{camera_id}/stream       a live microphone

AUTHENTICATION is the edge_id, exactly as every other control surface on this
device does it (api.control_auth) — the value only the app server that
provisioned this device knows. The WebSocket carries it as a QUERY parameter
because a browser cannot set headers on a WebSocket handshake; that is the same
choice the recordings endpoints already made, for the same reason.

This matters more here than anywhere else in the API: this endpoint is reachable
through the fleet tunnel, and it makes a noise in someone's home. So the edge_id
is checked before the camera is ever dialled, and a session is refused — never
queued — if that camera already has a speaker.

The socket IS accepted before a refusal, deliberately. Closing a WebSocket before
accept() is, by the ASGI spec, an HTTP 403 on the handshake: the browser sees
code 1006 and an EMPTY reason, whatever we meant to say. Until 2026-09-22 every
refusal here went out that way, so a wrong password looked exactly like a network
blip — the client retried it, each retry was another refused login on the
camera, and nobody could see why. Accepting first costs nothing (the camera is
not touched until the checks pass) and lets every refusal carry its real code
and sentence.

The wire is deliberately dumb: the client sends raw 8 kHz mono G.711 A-law as
binary frames, and JSON text frames for control. The browser does the encoding
(static/talk-worklet.js) because it already has the samples, and encoding there
costs a device nothing.
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect

from api.control_auth import check_edge_id, field
from config.settings import settings
from talkback import credentials, guard
from talkback.readiness import readiness
from talkback.sessions import hub
from talkback.mpegts import FRAME_BYTES, SAMPLE_RATE
from talkback.protocol import TalkbackError

logger = logging.getLogger("talkback.api")

router = APIRouter(prefix="/api/v1/talkback", tags=["Talkback"])

# Close codes the browser reads back to the user. 4000+ is the application range.
WS_UNAUTHORIZED = 4401
WS_BUSY = 4409
WS_PAUSED = 4429          # talkback.guard: refused locally to protect the camera
WS_UNAVAILABLE = 4503
WS_FAILED = 4500

# The WebSocket protocol caps a close reason at 123 BYTES. Our sentences carry
# multi-byte characters (an em dash is three), so a character count is not a
# byte count — and an over-long reason fails the close itself.
_REASON_BYTES = 120


def _reason(code: str, message: str) -> str:
    raw = f"{code}: {message}".encode("utf-8")[:_REASON_BYTES]
    return raw.decode("utf-8", "ignore")


async def _refuse(websocket: WebSocket, close_code: int, code: str, message: str) -> None:
    """Tell the client why: a JSON frame with the FULL sentence, then a close
    whose code and (truncated) reason say the same. The socket must already be
    accepted; see the module docstring for why that matters."""
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
        await websocket.close(code=close_code, reason=_reason(code, message))
    except Exception:
        pass

# One chunk of speech is at most this many bytes of A-law. A frame far larger
# than a mouthful of audio is a client bug or an attempt to flood the camera.
_MAX_CHUNK_BYTES = FRAME_BYTES * 50          # 1 second


def _http(exc: TalkbackError) -> HTTPException:
    """Map a protocol failure to the status that says the same thing in HTTP."""
    status = {
        "no_camera": 404,
        "no_credential": 428,        # Precondition Required: commission it first
        "no_host": 409,
        "busy": 409,
        # NOT 401. A 401 from this API means YOUR edge_id is wrong, and clients
        # reasonably react to it by logging out or refreshing credentials. The
        # camera refusing ITS password is a failed dependency, not your auth.
        "unauthorized": 424,
        "cooldown": 429,
        "unreachable": 502,
        "refused": 502,
        "timeout": 504,
        "stalled": 504,
        "protocol": 502,
        "closed": 502,
    }.get(exc.code, 500)
    headers = {"Retry-After": "900"} if exc.code == "cooldown" else None
    return HTTPException(status, detail={"code": exc.code, "message": str(exc)},
                         headers=headers)


def _require_enabled() -> None:
    if not settings.talkback_enabled:
        raise HTTPException(
            503, detail={"code": "disabled",
                         "message": "Talk-back is switched off on this device "
                                    "(TALKBACK_ENABLED)."})


@router.get("/cameras")
def list_cameras(edge_id: str | None = Query(None)) -> dict:
    """Every camera and whether it is ready to be talked to. Read-only, no
    network access — safe to call on every page load.

    Authenticated like every other control surface even though it only reads:
    this router is reachable from the internet through the fleet tunnel, and the
    list names the rooms in someone's home and the address of every camera in
    it. On a device with no edge_id yet (LAN development) check_edge_id is a
    no-op, so this does not get in the way of a bench."""
    check_edge_id(edge_id)
    return {
        "enabled": settings.talkback_enabled,
        # The ONE TP-Link account password for this home, set once in setup.
        "home_configured": credentials.home_configured(),
        "cameras": [{**c, "busy": hub.busy(c["camera_id"]),
                     # What the device found the last time it checked, silently.
                     # This is what lets the page say "needs the password"
                     # BEFORE a carer presses, rather than after.
                     "readiness": readiness.get(c["camera_id"])}
                    for c in hub.cameras()],
        "active": hub.status(),
    }


@router.get("/health")
def health(edge_id: str | None = Query(None)) -> dict:
    """What talk-back is doing right now, in numbers.

    Separate from /cameras because /cameras backs a page that renders on load
    and must stay cheap and cacheable, while this is a live gauge: queue depth,
    dropped frames, who is holding which room. `queued_ms` is the one number
    that answers "why does it sound delayed" — it is milliseconds of the
    carer's voice still waiting in the socket to the camera."""
    check_edge_id(edge_id)
    active = hub.status()
    return {
        "enabled": settings.talkback_enabled,
        "home_configured": credentials.home_configured(),
        "active_sessions": len(active),
        "active": active,
        "readiness": readiness.all(),
        # Cameras paused after repeated refusals (talkback.guard), so support can
        # see at once why a camera is not being asked.
        "guard": guard.snapshot(),
        "limits": {
            "hold_secs": settings.talkback_hold_secs,
            "max_turn_secs": settings.talkback_max_turn_secs,
            "sample_rate": SAMPLE_RATE,
            "frame_bytes": FRAME_BYTES,
            "codec": "alaw",
        },
    }


# How long a "set the password" or "check now" request waits for the verdicts.
# A camera check is ~0.1-1.5 s, or the timeout for one that is offline; past this
# the answer comes back without the stragglers rather than hanging the page.
_CHECK_WAIT_SECS = 30.0


async def _verdicts() -> dict:
    try:
        return await asyncio.wait_for(readiness.check_now(), _CHECK_WAIT_SECS)
    except asyncio.TimeoutError:
        return readiness.all()


def _summary(verdicts: dict) -> dict:
    states = [v.get("state") for v in verdicts.values()]
    return {"ready": states.count("ready"), "total": len(states),
            "all_ready": bool(states) and all(s == "ready" for s in states)}


@router.put("/credential")
async def set_home_credential(body: dict = Body(...)) -> dict:
    """Set THE TP-Link account password for this home, once, for every camera.

    Every camera in a home is paired to one TP-Link account, so this is the only
    password talk-back needs — typed once in camera setup, used by every camera
    including ones added later. Replaces the per-camera entries an operator had
    typed (they are the stale copies this supersedes), then checks every camera
    silently and answers with what each one said."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    password = field(body, "password", "cloudPassword", "cloud_password",
                     "tplinkPassword", default="")
    try:
        cleared = credentials.set_home_password(password)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid", "message": str(exc)})
    verdicts = await _verdicts()
    return {"configured": True, "scope": "home",
            "updated_at": credentials.home_updated_at(),
            "replaced_camera_overrides": cleared,
            "cameras": verdicts, **_summary(verdicts)}


@router.delete("/credential")
def forget_home_credential(edge_id: str | None = Query(None)) -> dict:
    _require_enabled()
    check_edge_id(edge_id)
    removed = credentials.forget_home()
    readiness.kick()
    return {"configured": False, "scope": "home", "removed": removed}


@router.post("/check")
async def check_all(body: dict = Body(default={})) -> dict:
    """Check every camera's speaker NOW, silently, and report. The device already
    does this on its own from boot; this is the "check again" button — for
    right after re-pairing a camera in the Tapo app."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    verdicts = await _verdicts()
    return {"cameras": verdicts, **_summary(verdicts)}


@router.put("/{camera_id}/credential")
def set_credential(camera_id: str, body: dict = Body(...)) -> dict:
    """Commission one camera for talk-back.

    The plaintext password is hashed here and discarded; nothing stores it,
    logs it, or can read it back. See talkback.credentials."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    password = field(body, "password", "cloudPassword", "cloud_password", default="")
    # Stored under the id the camera is KEPT under, or a password saved for
    # "living_room" is never found when LIVING_ROOM is dialled.
    camera_id = hub.canonical(camera_id)
    try:
        credentials.set_password(camera_id, password)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid", "message": str(exc)})
    readiness.kick()
    return {"camera_id": camera_id, "configured": True, "scope": "camera",
            "updated_at": credentials.updated_at(camera_id)}


@router.delete("/{camera_id}/credential")
def forget_credential(camera_id: str, edge_id: str | None = Query(None)) -> dict:
    _require_enabled()
    check_edge_id(edge_id)
    camera_id = hub.canonical(camera_id)
    removed = credentials.forget(camera_id)
    readiness.kick()
    return {"camera_id": camera_id, "configured": False, "removed": removed}


@router.post("/{camera_id}/test")
async def test_camera(camera_id: str, body: dict = Body(default={})) -> dict:
    """Open a speaker session and close it again WITHOUT sending audio — the
    handover check that proves the credential and the firmware without startling
    anyone in the room."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    camera_id = hub.canonical(camera_id)
    # `force` skips the lock-out pause (talkback.guard). For a technician who has
    # just fixed the account and wants the answer now; never for a carer.
    force = str(field(body, "force", default="")).lower() in ("1", "true", "yes")
    try:
        result = await hub.probe(camera_id, force=force)
    except TalkbackError as exc:
        # A test IS a check: what it found is the camera's readiness now, so a
        # per-camera "Test" button and the live wall agree immediately.
        readiness.observe(camera_id, exc.code, str(exc))
        raise _http(exc)
    readiness.observe(camera_id)
    return result


@router.websocket("/{camera_id}/stream")
async def talk_stream(websocket: WebSocket, camera_id: str) -> None:
    """A live microphone.

    Client -> server: binary frames of 8 kHz mono A-law (20 ms each is ideal),
    plus an optional `{"type":"stop"}` text frame for a clean hang-up.
    Server -> client: `{"type":"open"|"error"}` and periodic `{"type":"stats"}`,
    so the page can show a real ON AIR state instead of hoping.
    """
    edge_id = websocket.query_params.get("edge_id") or websocket.query_params.get("edgeId")
    # The browser's own id for THIS microphone. It is what lets a carer who
    # dropped off the network reclaim their own session instead of being told
    # the room is busy by a socket that no longer exists. Bounded, because it
    # is echoed into logs and into the busy message other carers read.
    client_id = (websocket.query_params.get("client_id")
                 or websocket.query_params.get("clientId") or "")[:64]
    # A display name for the "someone is already speaking" message other carers
    # see. Supplied by the client, so it is only ever DISPLAYED: bounded, and
    # stripped of anything that is not a printable character.
    label = "".join(ch for ch in (websocket.query_params.get("name") or "")
                    if ch.isprintable())[:40].strip()
    camera_id = hub.canonical(camera_id)

    # Accept FIRST, so a refusal can say why (module docstring). Nothing below
    # touches the camera until the edge_id has been checked.
    await websocket.accept()
    if not settings.talkback_enabled:
        await _refuse(websocket, WS_UNAVAILABLE, "disabled",
                      "Talk-back is switched off on this device.")
        return
    try:
        check_edge_id(edge_id)
    except HTTPException:
        await _refuse(websocket, WS_UNAUTHORIZED, "edge_id",
                      "This device did not accept the request (edge_id missing "
                      "or for another device).")
        return

    # Who to name in the "someone else is already speaking" message. Behind the
    # fleet tunnel every socket arrives from the proxy, so the peer address
    # would name Caddy at every house — the forwarded address is the only one
    # that identifies an actual person.
    forwarded = (websocket.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    holder = forwarded or (websocket.client.host if websocket.client else "unknown")
    try:
        session = await hub.open(camera_id, holder=holder, client_id=client_id,
                                 label=label)
        readiness.observe(camera_id)
    except TalkbackError as exc:
        readiness.observe(camera_id, exc.code, str(exc))
        await _refuse(websocket,
                      {"busy": WS_BUSY, "cooldown": WS_PAUSED}.get(exc.code, WS_FAILED),
                      exc.code, str(exc))
        logger.info("talk refused on %s: %s", camera_id, exc)
        return

    # FROM HERE ON the camera's speaker is ours, and it is given back in ONE
    # place: the `finally` below. Everything after hub.open() lives inside that
    # try — including telling the client it is open. That send used to sit
    # outside it, and a carer whose page went away during the ~200 ms camera
    # handshake (a reload, a closed tab, a dropped network) made it raise: the
    # handler left, the release never ran, and the camera stayed "busy" with
    # zero frames until the service restarted. On 2026-09-22 that held LOUNGE for
    # ten minutes and told every carer "someone is already speaking".
    started = time.monotonic()
    last_audio = started
    last_stats = started
    # When the CURRENT run of continuous speech began. Reset by any real gap, so
    # the stuck-button ceiling measures an open microphone rather than a held
    # connection — those are now different things (see talkback_hold_secs).
    talking_since = 0.0
    reason = "client closed"
    # How the socket is closed at the end. A normal 1000 tells the client "done,
    # do not come back" — right for a hold window running out, WRONG for a
    # camera that stopped reading mid-sentence, where a reconnect onto a clean
    # socket is exactly the recovery (protocol._QUEUE_DEAD_MS).
    close_code, close_reason = 1000, ""
    # ONE long-lived receive task, waited on rather than cancelled. A fresh
    # wait_for(receive()) per iteration would cancel a receive mid-frame every
    # time a ceiling is re-checked, and a cancelled receive can take a frame of
    # someone's voice with it.
    pending = None
    try:
        await websocket.send_json({"type": "open", "camera_id": camera_id,
                                   "session_id": session.session_id,
                                   "sample_rate": SAMPLE_RATE, "codec": "alaw",
                                   "frame_bytes": FRAME_BYTES,
                                   "mic_gain": settings.talkback_mic_gain,
                                   "hold_secs": settings.talkback_hold_secs,
                                   "client_id": client_id,
                                   "max_turn_secs": settings.talkback_max_turn_secs})
        logger.info("talk open on %s for %s", camera_id, holder)
        pending = asyncio.ensure_future(websocket.receive())
        while True:
            now = time.monotonic()
            hold_left = settings.talkback_hold_secs - (now - last_audio)
            turn_left = (settings.talkback_max_turn_secs - (now - talking_since)
                         if talking_since else float("inf"))
            if hold_left <= 0:
                reason = "held too long without speech"
                break
            if turn_left <= 0:
                reason = "max turn length"
                break
            done, _ = await asyncio.wait({pending},
                                         timeout=min(hold_left, turn_left))
            if not done:
                continue                        # a ceiling came due; re-check it
            message = pending.result()
            pending = asyncio.ensure_future(websocket.receive())
            if message["type"] == "websocket.disconnect":
                break

            chunk = message.get("bytes")
            if chunk is None:
                # Text frames are control, never audio: they must NOT refresh the
                # hold window, or a chatty client could hold a household's
                # speaker forever without saying a word.
                text = (message.get("text") or "").strip()
                if '"stop"' in text:
                    reason = "client stopped"
                    break
                continue
            if not chunk or len(chunk) > _MAX_CHUNK_BYTES:
                continue                        # keep-alive, or a client bug
            await session.send(chunk)
            hub.note_frame(camera_id)
            now = time.monotonic()
            # A gap longer than a few frames means the button was released, so
            # the next word starts a NEW turn against the stuck-button ceiling.
            if not talking_since or now - last_audio > 1.0:
                talking_since = now
            last_audio = now

            if last_audio - last_stats >= 1.0:
                last_stats = last_audio
                await websocket.send_json({
                    "type": "stats",
                    "seconds": round(last_audio - started, 1),
                    "bytes": session.bytes_sent,
                    # The channel's own health, on the same frame the page is
                    # already reading. A carer should not have to poll a second
                    # endpoint to find out their voice is arriving late.
                    **session.health()})
    except (WebSocketDisconnect, OSError):
        # The carer left — including before the "open" frame could be sent.
        reason = "disconnected"
    except TalkbackError as exc:
        reason = exc.code
        logger.warning("talk failed on %s: %s", camera_id, exc)
        close_code, close_reason = WS_FAILED, _reason(exc.code, str(exc))
        try:
            await websocket.send_json({"type": "error", "code": exc.code,
                                       "message": str(exc)})
        except Exception:
            pass
    except Exception:
        reason = "internal"
        logger.exception("talk stream crashed on %s", camera_id)
    finally:
        if pending is not None:
            pending.cancel()
        # The camera's speaker is released on EVERY exit path — a crash here
        # would otherwise lock a household out of its own intercom.
        await hub.release(camera_id, session)
        try:
            await websocket.close(code=close_code, reason=close_reason)
        except Exception:
            pass
        logger.info("talk closed on %s after %.1fs (%s)",
                    camera_id, time.monotonic() - started, reason)
