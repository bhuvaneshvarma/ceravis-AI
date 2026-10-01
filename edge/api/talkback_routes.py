from __future__ import annotations

"""
Talk-back API — speaking into a room through its camera, and setting it up.

    GET    /api/v1/talkback/cameras                  cameras, readiness, who has the floor
    GET    /api/v1/talkback/health                   lines, floors, the line record
    GET    /api/v1/talkback/log                      who spoke into which room, when, how long
    PUT    /api/v1/talkback/credential               THE home's TP-Link password
    DELETE /api/v1/talkback/credential               forget it
    POST   /api/v1/talkback/check                    look at every camera now
    PUT    /api/v1/talkback/{camera_id}/credential   override for one camera
    DELETE /api/v1/talkback/{camera_id}/credential   forget the override
    POST   /api/v1/talkback/{camera_id}/test         is this camera's line open
    WS     /api/v1/talkback/{camera_id}/stream       a carer's microphone, into that camera

THE STREAM. One socket per carer per camera. Connecting claims that camera's
floor; {"type":"open"} says it is granted. Binary 20 ms A-law frames are speech,
for as long as the button is held; {"type":"release"} (or just silence) lets the
floor go after the floor hold. The socket may stay open between presses — the
next press is instant — without holding the room. The camera side is the edge's
own always-open LINE (talkback.lines), so no press waits for a camera handshake.
A refusal is always an {"type":"error"} frame followed by a coded close.

AUTHENTICATION is the edge_id, exactly as every other control surface on this
device (api.control_auth). Through the fleet it is already in the URL —
/<edge_id>/api/v1/talkback/… reaches this device only because of it — so there
the `edge_id` parameter is optional; one that IS sent must still match. A direct
LAN call has no prefix and must send it. (A WebSocket can only carry it in the
URL: a browser cannot set headers on the handshake.)

The socket is ACCEPTED before a refusal. Closing a WebSocket before accept() is,
by the ASGI spec, an HTTP 403 on the handshake: the browser sees 1006 and an empty
reason whatever we meant to say. Accepting first costs nothing (the edge_id is
checked before anything is done) and lets a refusal carry its real code.
"""

import asyncio
import json
import logging
import time
import uuid

from fastapi import (APIRouter, Body, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect)

from api.control_auth import check_edge_id, field
from config.settings import settings
from talkback import audit, credentials, guard
from talkback.lines import lines
from talkback.mpegts import FRAME_BYTES, SAMPLE_RATE
from talkback.protocol import TalkbackError
from talkback.sessions import IDLE_CLOSE_SECS, Talker, hub, is_speech

logger = logging.getLogger("talkback.api")

router = APIRouter(prefix="/api/v1/talkback", tags=["Talkback"])

# Close codes. A refusal is an {"type":"error"} frame, then one of these.
WS_UNAUTHORIZED = 4401    # edge_id missing or for another device
WS_BUSY = 4409            # another carer has this camera's floor (or took it)
WS_COOLDOWN = 4429        # paused after repeated refused passwords (talkback.guard)
WS_FAILED = 4500          # anything else: the reason's `code:` prefix says what
WS_UNAVAILABLE = 4503     # talk-back is switched off on this device

# The WebSocket protocol caps a close reason at 123 BYTES; our sentences carry
# multi-byte characters, so they are cut by bytes, never by characters.
_REASON_BYTES = 120
# One chunk of speech is at most this many bytes of A-law (1 s). Anything larger
# is a client bug or an attempt to flood the camera.
_MAX_CHUNK_BYTES = FRAME_BYTES * 50
_MAX_TEXT_BYTES = 2048


def _reason(code: str, message: str) -> str:
    raw = f"{code}: {message}".encode("utf-8")[:_REASON_BYTES]
    return raw.decode("utf-8", "ignore")


def _http(exc: TalkbackError) -> HTTPException:
    """Map a talk-back failure to the status that says the same thing in HTTP."""
    status = {
        "no_camera": 404,
        "no_credential": 428,        # Precondition Required: commission it first
        # NOT 401: a 401 from this API means YOUR edge_id is wrong, and clients
        # react to it by logging out. The camera refusing ITS password is a
        # failed dependency.
        "unauthorized": 424,
        "cooldown": 429,
        "unreachable": 502,
        "refused": 502,
        "protocol": 502,
        "timeout": 504,
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


def _camera_rows() -> list[dict]:
    return [{**c, "busy": hub.busy(c["camera_id"]),
             "floor": hub.floor(c["camera_id"]),
             # What the camera's line says right now. This is what lets a page
             # say "needs the password" BEFORE a carer presses, not after.
             "readiness": lines.readiness(c["camera_id"])}
            for c in hub.cameras()]


@router.get("")
@router.get("/")
@router.get("/cameras")
def list_cameras(request: Request, edge_id: str | None = Query(None)) -> dict:
    """Every camera, whether it can be talked to, and who holds its floor.
    Read-only and cheap — safe on every page load. Authenticated even though it
    only reads: it names the rooms in someone's home.

    Also answers on the bare /talkback — the address a client naturally asks
    for "the talk-back state" — so both spellings are the SAME call. Both "" and
    "/" are declared: a trailing-slash redirect would be built from the path the
    fleet prefix was already stripped from, and send a fleet client nowhere."""
    check_edge_id(edge_id, request.scope)
    return {
        "enabled": settings.talkback_enabled,
        "home_configured": credentials.home_configured(),
        "cameras": _camera_rows(),
    }


@router.get("/log")
def talk_log(request: Request, edge_id: str | None = Query(None),
             camera: str | None = Query(None), event: str = Query("talk,refused"),
             limit: int = Query(100, ge=1, le=1000)) -> dict:
    """Who spoke into which room, when and for how long — and who was refused.
    Newest first. `event` picks the kinds: talk, refused (the default, as the
    app teams were given it), session (each talk socket as a call and its
    response, with an audio check), or `all`."""
    check_edge_id(edge_id, request.scope)
    kinds = None if event.strip() == "all" else {e.strip() for e in event.split(",") if e.strip()}
    return {"entries": audit.recent(hub.canonical(camera) if camera else None, limit, kinds)}


@router.get("/health")
def health(request: Request, edge_id: str | None = Query(None)) -> dict:
    """What talk-back is doing right now, in numbers — and the LINE RECORD: for
    every camera, how long each connection to its speaker lasted and why it
    ended. That record is the answer to "how long does a camera hold the line".
    `queued_ms` is the answer to "why does it sound delayed"."""
    check_edge_id(edge_id, request.scope)
    return {
        "enabled": settings.talkback_enabled,
        "home_configured": credentials.home_configured(),
        "settings": {
            "line_always": settings.talkback_line_always,
            "line_keepalive_secs": settings.talkback_line_keepalive_secs,
            "floor_hold_secs": settings.talkback_floor_hold_secs,
            "codec": "alaw", "sample_rate": SAMPLE_RATE, "frame_bytes": FRAME_BYTES,
        },
        "lines": lines.health(),
        **hub.status(),
        # Cameras paused after repeated refused logins (talkback.guard).
        "guard": guard.snapshot(),
    }


# How long a "set the password" or "check now" request waits for the verdicts.
_CHECK_WAIT_SECS = 30.0


async def _verdicts() -> dict:
    try:
        return await asyncio.wait_for(lines.check_now(), _CHECK_WAIT_SECS)
    except asyncio.TimeoutError:
        return lines.all()


def _summary(verdicts: dict) -> dict:
    states = [v.get("state") for v in verdicts.values()]
    return {"ready": states.count("ready"), "total": len(states),
            "all_ready": bool(states) and all(s == "ready" for s in states)}


@router.put("/credential")
async def set_home_credential(request: Request, body: dict = Body(...)) -> dict:
    """Set THE TP-Link account password for this home, once, for every camera —
    then open every camera's line and answer with what each one said."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"), request.scope)
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
def forget_home_credential(request: Request, edge_id: str | None = Query(None)) -> dict:
    _require_enabled()
    check_edge_id(edge_id, request.scope)
    removed = credentials.forget_home()
    lines.kick()
    return {"configured": False, "scope": "home", "removed": removed}


@router.post("/check")
async def check_all(request: Request, body: dict = Body(default={})) -> dict:
    """Look at every camera NOW — the "check again" button, for right after
    re-pairing a camera in the Tapo app. Lines are kept open anyway; this only
    retries the ones that are not."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"), request.scope)
    verdicts = await _verdicts()
    return {"cameras": verdicts, **_summary(verdicts)}


@router.put("/{camera_id}/credential")
def set_credential(camera_id: str, request: Request, body: dict = Body(...)) -> dict:
    """An override for one camera paired to a different TP-Link account. The
    plaintext is hashed here and discarded (talkback.credentials)."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"), request.scope)
    password = field(body, "password", "cloudPassword", "cloud_password", default="")
    camera_id = hub.canonical(camera_id)
    try:
        credentials.set_password(camera_id, password)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid", "message": str(exc)})
    lines.kick(camera_id)
    return {"camera_id": camera_id, "configured": True, "scope": "camera",
            "updated_at": credentials.updated_at(camera_id)}


@router.delete("/{camera_id}/credential")
def forget_credential(camera_id: str, request: Request,
                      edge_id: str | None = Query(None)) -> dict:
    _require_enabled()
    check_edge_id(edge_id, request.scope)
    camera_id = hub.canonical(camera_id)
    removed = credentials.forget(camera_id)
    lines.kick(camera_id)
    return {"camera_id": camera_id, "configured": False, "removed": removed}


@router.post("/{camera_id}/test")
async def test_camera(camera_id: str, request: Request,
                      body: dict = Body(default={})) -> dict:
    """Is this camera's line open — and if not, can it be opened now? Silent: a
    line carries no sound until somebody speaks. `force` skips the lock-out
    pause, for a technician who has just fixed the account."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"), request.scope)
    force = str(field(body, "force", default="")).lower() in ("1", "true", "yes")
    try:
        return await lines.test(hub.canonical(camera_id), force=force)
    except TalkbackError as exc:
        raise _http(exc)


def _identity(value: str | None, limit: int) -> str:
    """A client-supplied label, only ever displayed and logged: printable and
    bounded."""
    return "".join(ch for ch in (value or "") if ch.isprintable())[:limit].strip()


@router.websocket("/{camera_id}/stream")
async def talk_stream(websocket: WebSocket, camera_id: str) -> None:
    """A carer's microphone into one camera. Every socket — granted, refused or
    dropped — ends as ONE `session` entry in the talk log (talkback.audit): the
    call, the edge's answer, how it closed, and what audio actually arrived."""
    q = websocket.query_params
    edge_id = q.get("edge_id") or q.get("edgeId")
    # The carer's control: sent again on a reconnect, it gets its floor back.
    client_id = _identity(q.get("client_id") or q.get("clientId"), 64) \
        or "srv-" + uuid.uuid4().hex[:12]
    forwarded = (websocket.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    holder = forwarded or (websocket.client.host if websocket.client else "unknown")
    name = _identity(q.get("name") or q.get("userName"), 40)
    user_id = _identity(q.get("user_id") or q.get("userId"), 64)

    await websocket.accept()
    log = audit.Session(hub.canonical(camera_id), dict(q), holder=holder,
                        user_agent=websocket.headers.get("user-agent", ""),
                        name=name, user_id=user_id, client_id=client_id)
    try:
        await _talk(websocket, log, camera_id, edge_id, client_id, name, user_id, holder)
    finally:
        log.finish()


async def _talk(websocket: WebSocket, log: audit.Session, camera_id: str,
                edge_id: str | None, client_id: str, name: str, user_id: str,
                holder: str) -> None:
    if not settings.talkback_enabled:
        return await _refuse(websocket, "disabled",
                             "Talk-back is switched off on this device.", log)
    try:
        check_edge_id(edge_id, websocket.scope)
    except HTTPException:
        return await _refuse(websocket, "edge_id",
                             "This device did not accept the request (edge_id "
                             "missing or for another device).", log)

    async def close(code: int, reason: str) -> None:
        log.closed(code, "edge", reason)
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    talker = Talker(
        camera_id=hub.canonical(camera_id), client_id=client_id, name=name,
        user_id=user_id, holder=holder,
        end=lambda code, message: _refuse(websocket, code, message, log), close=close)
    refusal = await hub.open(talker)
    if refusal is not None:
        return await _refuse(websocket, refusal["code"], refusal["message"], log)
    logger.info("talk %s open for %s (%s)", talker.camera_id, name or holder, client_id)
    last_stats = 0.0
    try:
        opened = {
            "type": "open", "camera_id": talker.camera_id, "client_id": client_id,
            "codec": "alaw", "sample_rate": SAMPLE_RATE, "frame_bytes": FRAME_BYTES,
            "mic_gain": settings.talkback_mic_gain,
            # How long this socket may idle between presses before the edge
            # closes it; the floor itself is only held floor_hold_secs.
            "hold_secs": IDLE_CLOSE_SECS,
            "floor_hold_secs": settings.talkback_floor_hold_secs,
            "max_turn_secs": 0,                    # none: talk while it is held
            "frames_per_sec": SAMPLE_RATE // FRAME_BYTES,   # 50 pieces = 1 s of voice
        }
        await websocket.send_json(opened)
        log.answered(opened)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                log.closed(message.get("code"), "client")
                break
            chunk = message.get("bytes")
            if chunk is not None:
                if not 0 < len(chunk) <= _MAX_CHUNK_BYTES:
                    continue
                log.frame(len(chunk), is_speech(chunk))
                refusal = await hub.audio(talker, chunk)
                if refusal is not None:
                    await _refuse(websocket, refusal["code"], refusal["message"], log)
                    break
                now = time.monotonic()
                if now - last_stats >= 1.0 and hub.floor(talker.camera_id).get(
                        "client_id") == client_id:
                    last_stats = now
                    # `received_hz`: samples of YOUR audio the edge received per
                    # second since the last stats (8000 = right). The rest is the
                    # camera link's running health.
                    await websocket.send_json({"type": "stats",
                                               "received_hz": log.window_hz(),
                                               "expected_hz": SAMPLE_RATE,
                                               **lines.session_health(talker.camera_id)})
                continue
            text = message.get("text") or ""
            if len(text) > _MAX_TEXT_BYTES:
                continue
            try:
                command = json.loads(text)
            except ValueError:
                continue
            kind = command.get("type") if isinstance(command, dict) else None
            if kind == "release":
                log.released()
                hub.release(talker)
            elif kind == "stop":
                log.closed(1000, "client", "stop")
                break
            # "ping" and anything unknown: nothing to do; the frame kept the
            # connection alive through the proxies, which was its only job.
    except (WebSocketDisconnect, OSError, RuntimeError):
        log.closed(None, "client", "connection lost")   # the carer left mid-stream
    except Exception:
        logger.exception("talk stream crashed for %s", client_id)
        log.closed(1011, "edge", "crashed")
    finally:
        # Every exit lets the floor go (held for the floor hold, so a carer
        # whose network dropped can come back to it).
        await hub.leave(talker)
        await close(1000, "")
        logger.info("talk %s closed for %s (%s)", talker.camera_id, name or holder, client_id)


_CLOSE_FOR = {"edge_id": WS_UNAUTHORIZED, "disabled": WS_UNAVAILABLE,
              "busy": WS_BUSY, "taken": WS_BUSY, "cooldown": WS_COOLDOWN}


async def _refuse(websocket: WebSocket, code: str, message: str,
                  log: audit.Session) -> None:
    """Say why — the full sentence in a frame, then a close whose code and
    (byte-capped) reason say the same. Both go into the session's log entry."""
    error = {"type": "error", "code": code, "message": message}
    close_code = _CLOSE_FOR.get(code, WS_FAILED)
    log.answered(error)
    log.closed(close_code, "edge", code)
    try:
        await websocket.send_json(error)
        await websocket.close(code=close_code, reason=_reason(code, message))
    except Exception:
        pass
