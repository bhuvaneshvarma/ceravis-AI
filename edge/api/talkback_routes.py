from __future__ import annotations

"""
Talk-back API — speaking into a room through its camera, and setting it up.

    GET    /api/v1/talkback/cameras                  cameras, readiness, who has the floor
    GET    /api/v1/talkback/health                   lines, floors, the line record
    PUT    /api/v1/talkback/credential               THE home's TP-Link password
    DELETE /api/v1/talkback/credential               forget it
    POST   /api/v1/talkback/check                    look at every camera now
    PUT    /api/v1/talkback/{camera_id}/credential   override for one camera
    DELETE /api/v1/talkback/{camera_id}/credential   forget the override
    POST   /api/v1/talkback/{camera_id}/test         is this camera's line open
    WS     /api/v1/talkback/session                  a page's standby connection

THE SESSION. A page opens ONE WebSocket when it loads and keeps it. It claims
nothing and blocks nobody. On a press it sends {"type":"claim","camera":…}; once
granted, binary 20 ms A-law frames go to that camera; {"type":"release"} when the
button comes up. The edge pushes floor and camera changes to every page, so no
page polls. The camera side is the edge's own always-open LINE (talkback.lines)
and the right to speak is the FLOOR (talkback.sessions) — a press waits for
neither a connection nor a camera handshake.

AUTHENTICATION is the edge_id, exactly as every other control surface on this
device (api.control_auth). The WebSocket carries it as a query parameter because
a browser cannot set headers on a WebSocket handshake.

The socket is ACCEPTED before a refusal. Closing a WebSocket before accept() is,
by the ASGI spec, an HTTP 403 on the handshake: the browser sees 1006 and an empty
reason whatever we meant to say. Accepting first costs nothing (the edge_id is
checked before anything is done) and lets a refusal carry its real code.
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect

from api.control_auth import check_edge_id, field
from config.settings import settings
from talkback import credentials, guard
from talkback.lines import lines
from talkback.mpegts import FRAME_BYTES, SAMPLE_RATE
from talkback.protocol import TalkbackError
from talkback.sessions import Client, hub

logger = logging.getLogger("talkback.api")

router = APIRouter(prefix="/api/v1/talkback", tags=["Talkback"])

# Close codes for the SESSION connection itself. Floor refusals are messages, not
# closes: the standby connection outlives any one press.
WS_REPLACED = 4000        # the same page connected again; this socket is the stale one
WS_UNAUTHORIZED = 4401
WS_UNAVAILABLE = 4503

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


@router.get("/cameras")
def list_cameras(edge_id: str | None = Query(None)) -> dict:
    """Every camera, whether it can be talked to, and who holds its floor.
    Read-only and cheap — safe on every page load. Authenticated even though it
    only reads: it names the rooms in someone's home."""
    check_edge_id(edge_id)
    return {
        "enabled": settings.talkback_enabled,
        "home_configured": credentials.home_configured(),
        "cameras": _camera_rows(),
    }


@router.get("/health")
def health(edge_id: str | None = Query(None)) -> dict:
    """What talk-back is doing right now, in numbers — and the LINE RECORD: for
    every camera, how long each connection to its speaker lasted and why it
    ended. That record is the answer to "how long does a camera hold the line".
    `queued_ms` is the answer to "why does it sound delayed"."""
    check_edge_id(edge_id)
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
async def set_home_credential(body: dict = Body(...)) -> dict:
    """Set THE TP-Link account password for this home, once, for every camera —
    then open every camera's line and answer with what each one said."""
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
    lines.kick()
    return {"configured": False, "scope": "home", "removed": removed}


@router.post("/check")
async def check_all(body: dict = Body(default={})) -> dict:
    """Look at every camera NOW — the "check again" button, for right after
    re-pairing a camera in the Tapo app. Lines are kept open anyway; this only
    retries the ones that are not."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    verdicts = await _verdicts()
    return {"cameras": verdicts, **_summary(verdicts)}


@router.put("/{camera_id}/credential")
def set_credential(camera_id: str, body: dict = Body(...)) -> dict:
    """An override for one camera paired to a different TP-Link account. The
    plaintext is hashed here and discarded (talkback.credentials)."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
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
def forget_credential(camera_id: str, edge_id: str | None = Query(None)) -> dict:
    _require_enabled()
    check_edge_id(edge_id)
    camera_id = hub.canonical(camera_id)
    removed = credentials.forget(camera_id)
    lines.kick(camera_id)
    return {"camera_id": camera_id, "configured": False, "removed": removed}


@router.post("/{camera_id}/test")
async def test_camera(camera_id: str, body: dict = Body(default={})) -> dict:
    """Is this camera's line open — and if not, can it be opened now? Silent: a
    line carries no sound until somebody speaks. `force` skips the lock-out
    pause, for a technician who has just fixed the account."""
    _require_enabled()
    check_edge_id(field(body, "edgeId", "edge_id"))
    force = str(field(body, "force", default="")).lower() in ("1", "true", "yes")
    try:
        return await lines.test(hub.canonical(camera_id), force=force)
    except TalkbackError as exc:
        raise _http(exc)


@router.websocket("/session")
async def talk_session(websocket: WebSocket) -> None:
    """A page's standby connection: floor claims, speech, and live updates."""
    q = websocket.query_params
    edge_id = q.get("edge_id") or q.get("edgeId")
    # The page's own id. It survives the page's reconnects, so a carer whose
    # network dropped mid-sentence comes back to their own floor.
    client_id = (q.get("client_id") or q.get("clientId") or "")[:64] or hub.new_client_id()
    # Shown to other carers ("Nurse Priya is speaking"). The client supplies it,
    # so it is only ever DISPLAYED: bounded and stripped to printable characters.
    name = "".join(ch for ch in (q.get("name") or "") if ch.isprintable())[:40].strip()
    forwarded = (websocket.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    holder = forwarded or (websocket.client.host if websocket.client else "unknown")

    await websocket.accept()
    if not settings.talkback_enabled:
        return await _refuse(websocket, WS_UNAVAILABLE, "disabled",
                             "Talk-back is switched off on this device.")
    try:
        check_edge_id(edge_id)
    except HTTPException:
        return await _refuse(websocket, WS_UNAUTHORIZED, "edge_id",
                             "This device did not accept the request (edge_id "
                             "missing or for another device).")

    # Pushes come from this loop AND from other carers' actions (floor changes)
    # and the lines (camera changes); one lock keeps whole messages whole.
    send_lock = asyncio.Lock()

    async def push(message: dict) -> None:
        async with send_lock:
            await websocket.send_json(message)

    async def close(code: int, reason: str) -> None:
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    client = Client(client_id=client_id, name=name, holder=holder, push=push, close=close)
    await hub.connect(client)
    logger.info("talk session open for %s (%s)", name or holder, client_id)
    last_stats = 0.0
    try:
        await push({
            "type": "welcome", "client_id": client_id,
            "codec": "alaw", "sample_rate": SAMPLE_RATE, "frame_bytes": FRAME_BYTES,
            "mic_gain": settings.talkback_mic_gain,
            "floor_hold_secs": settings.talkback_floor_hold_secs,
            "cameras": {c["camera_id"]: {"readiness": c["readiness"], "floor": c["floor"]}
                        for c in _camera_rows()},
        })
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            chunk = message.get("bytes")
            if chunk is not None:
                if 0 < len(chunk) <= _MAX_CHUNK_BYTES:
                    camera = await hub.audio(client, chunk)
                    now = time.monotonic()
                    if camera and now - last_stats >= 1.0:
                        last_stats = now
                        await push({"type": "stats", "camera": camera,
                                    **lines.session_health(camera)})
                continue
            text = message.get("text") or ""
            if len(text) > _MAX_TEXT_BYTES:
                continue
            try:
                command = json.loads(text)
            except ValueError:
                continue
            kind = command.get("type") if isinstance(command, dict) else None
            if kind == "claim":
                await push(await hub.claim(client, str(command.get("camera") or "")))
            elif kind == "release":
                await hub.release(client)
            elif kind == "stop":
                break
            # "ping" and anything unknown: nothing to do; the frame kept the
            # connection alive through the proxies, which was its only job.
    except (WebSocketDisconnect, OSError):
        pass
    except Exception:
        logger.exception("talk session crashed for %s", client_id)
    finally:
        # Every exit gives the floor back (held for the floor hold, so a page that
        # reconnects in time carries on).
        await hub.disconnect(client)
        await close(1000, "")
        logger.info("talk session closed for %s (%s)", name or holder, client_id)


async def _refuse(websocket: WebSocket, close_code: int, code: str, message: str) -> None:
    """Say why — the full sentence in a frame, then a close whose code and
    (byte-capped) reason say the same."""
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
        await websocket.close(code=close_code, reason=_reason(code, message))
    except Exception:
        pass
