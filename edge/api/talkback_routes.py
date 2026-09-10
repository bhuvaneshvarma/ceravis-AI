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
through the fleet tunnel, and it makes a noise in someone's home. So the gate is
checked BEFORE the socket is accepted, and a session is refused — never queued —
if that camera already has a speaker.

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
from talkback import credentials
from talkback.sessions import hub
from talkback.mpegts import FRAME_BYTES, SAMPLE_RATE
from talkback.protocol import TalkbackError

logger = logging.getLogger("talkback.api")

router = APIRouter(prefix="/api/v1/talkback", tags=["Talkback"])

# Close codes the browser reads back to the user. 4000+ is the application range.
WS_UNAUTHORIZED = 4401
WS_BUSY = 4409
WS_UNAVAILABLE = 4503
WS_FAILED = 4500

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
        "unauthorized": 401,
        "unreachable": 502,
        "refused": 502,
        "timeout": 504,
        "stalled": 504,
        "protocol": 502,
        "closed": 502,
    }.get(exc.code, 500)
    return HTTPException(status, detail={"code": exc.code, "message": str(exc)})


def _require_enabled() -> None:
    if not settings.talkback_enabled:
        raise HTTPException(
            503, detail={"code": "disabled",
                         "message": "Talk-back is switched off on this device "
                                    "(TALKBACK_ENABLED)."})


@router.get("/cameras")
def list_cameras() -> dict:
    """Every camera and whether it is ready to be talked to. Read-only, no
    network access — safe to call on every page load."""
    return {
        "enabled": settings.talkback_enabled,
        "cameras": [{**c, "busy": hub.busy(c["camera_id"])} for c in hub.cameras()],
        "active": hub.status(),
    }


@router.put("/{camera_id}/credential")
def set_credential(camera_id: str, body: dict = Body(...)) -> dict:
    """Commission one camera for talk-back.

    The plaintext password is hashed here and discarded; nothing stores it,
    logs it, or can read it back. See talkback.credentials."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    password = field(body, "password", "cloudPassword", "cloud_password", default="")
    try:
        credentials.set_password(camera_id, password)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid", "message": str(exc)})
    return {"camera_id": camera_id, "configured": True,
            "updated_at": credentials.updated_at(camera_id)}


@router.delete("/{camera_id}/credential")
def forget_credential(camera_id: str, edge_id: str | None = Query(None)) -> dict:
    _require_enabled()
    check_edge_id(edge_id)
    return {"camera_id": camera_id, "configured": False,
            "removed": credentials.forget(camera_id)}


@router.post("/{camera_id}/test")
async def test_camera(camera_id: str, body: dict = Body(default={})) -> dict:
    """Open a speaker session and close it again WITHOUT sending audio — the
    handover check that proves the credential and the firmware without startling
    anyone in the room."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    try:
        return await hub.probe(camera_id)
    except TalkbackError as exc:
        raise _http(exc)


@router.websocket("/{camera_id}/stream")
async def talk_stream(websocket: WebSocket, camera_id: str) -> None:
    """A live microphone.

    Client -> server: binary frames of 8 kHz mono A-law (20 ms each is ideal),
    plus an optional `{"type":"stop"}` text frame for a clean hang-up.
    Server -> client: `{"type":"open"|"error"}` and periodic `{"type":"stats"}`,
    so the page can show a real ON AIR state instead of hoping.
    """
    edge_id = websocket.query_params.get("edge_id") or websocket.query_params.get("edgeId")

    # Everything that can be refused is refused BEFORE accept(), so a rejected
    # caller never reaches the camera and never holds its lock.
    if not settings.talkback_enabled:
        await websocket.close(code=WS_UNAVAILABLE, reason="talk-back disabled")
        return
    try:
        check_edge_id(edge_id)
    except HTTPException:
        await websocket.close(code=WS_UNAUTHORIZED, reason="edge_id required")
        return

    # Who to name in the "someone else is already speaking" message. Behind the
    # fleet tunnel every socket arrives from the proxy, so the peer address
    # would name Caddy at every house — the forwarded address is the only one
    # that identifies an actual person.
    forwarded = (websocket.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    holder = forwarded or (websocket.client.host if websocket.client else "unknown")
    try:
        session = await hub.open(camera_id, holder=holder)
    except TalkbackError as exc:
        await websocket.close(
            code=WS_BUSY if exc.code == "busy" else WS_FAILED,
            # Close reasons are capped at 123 bytes by the protocol.
            reason=f"{exc.code}: {exc}"[:120])
        logger.info("talk refused on %s: %s", camera_id, exc)
        return

    await websocket.accept()
    await websocket.send_json({"type": "open", "camera_id": camera_id,
                               "session_id": session.session_id,
                               "sample_rate": SAMPLE_RATE, "codec": "alaw",
                               "frame_bytes": FRAME_BYTES,
                               "mic_gain": settings.talkback_mic_gain,
                               "hold_secs": settings.talkback_hold_secs})
    logger.info("talk open on %s for %s", camera_id, holder)

    started = time.monotonic()
    last_audio = started
    last_stats = started
    # When the CURRENT run of continuous speech began. Reset by any real gap, so
    # the stuck-button ceiling measures an open microphone rather than a held
    # connection — those are now different things (see talkback_hold_secs).
    talking_since = 0.0
    reason = "client closed"
    # ONE long-lived receive task, waited on rather than cancelled. A fresh
    # wait_for(receive()) per iteration would cancel a receive mid-frame every
    # time a ceiling is re-checked, and a cancelled receive can take a frame of
    # someone's voice with it.
    pending = asyncio.ensure_future(websocket.receive())
    try:
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
                    "bytes": session.bytes_sent})
    except WebSocketDisconnect:
        reason = "disconnected"
    except TalkbackError as exc:
        reason = exc.code
        logger.warning("talk failed on %s: %s", camera_id, exc)
        try:
            await websocket.send_json({"type": "error", "code": exc.code,
                                       "message": str(exc)})
        except Exception:
            pass
    except Exception:
        reason = "internal"
        logger.exception("talk stream crashed on %s", camera_id)
    finally:
        pending.cancel()
        # The camera's speaker is released on EVERY exit path — a crash here
        # would otherwise lock a household out of its own intercom.
        await hub.release(camera_id, session)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("talk closed on %s after %.1fs (%s)",
                    camera_id, time.monotonic() - started, reason)
