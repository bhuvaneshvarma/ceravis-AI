from __future__ import annotations

"""
PTZ core — the ONE place a camera is physically moved.

Both callers come through here: the cloud label endpoint
(`POST /api/v1/cameras/ptz`) and the LAN setup pad
(`POST /api/v1/cameras/{camera_id}/ptz`). One code path, so the home
bookkeeping, the safety auto-stop and the console logging are identical no
matter who asked — see [[ceravis-one-mechanism-principle]].

Two rules define the whole contract:

  * **A cloud move is SELF-TERMINATING.** We start an ONVIF ContinuousMove and
    arm a timer that issues Stop after at most `ptz_max_move_ms`. A lost — or
    never sent — stop therefore cannot leave a motor spinning, which is exactly
    why the cloud API has no "stop" action: there is nothing left for a caller
    to stop. (The LAN installer pad passes `duration_ms=None` and owns its own
    stop, because a held button must keep moving.)

  * **The pre-override framing is remembered.** The position the camera held
    before the FIRST move of an override session is captured once and kept.
    `revert()` drives back to it and forgets it — that is the entire mechanism
    behind the cloud call's `action:"revert"`.

"""

import logging
import threading

from api.control_auth import canon
from config.settings import settings
from integration import call_log
from schemas.cameras import Camera


logger = logging.getLogger("cameras")

_lock = threading.RLock()
# camera_id -> the pending auto-stop timer for its in-flight move
_stop_timers: dict[str, threading.Timer] = {}
# camera_id -> (pan, tilt, zoom) the camera framed BEFORE this override began
_home: dict[str, tuple[float, float, float]] = {}


# ---- camera plumbing -------------------------------------------------

def token_for(camera: Camera) -> str:
    """The profile PTZ is bound to — it can differ from the one feeding the AI."""
    return camera.onvif_ptz_token or camera.onvif_profile_token or ""


def client(camera: Camera):
    """A fresh ONVIF client for this camera (imported lazily like every other
    ONVIF call site, so the SOAP layer stays off the boot path)."""
    from onvif.client import OnvifCamera
    return OnvifCamera(camera.onvif_xaddr, camera.onvif_username or "",
                       camera.onvif_password or "")


def has_ptz(camera: Camera) -> bool:
    """A camera can only be driven when discovery found PTZ *and* left us an
    ONVIF endpoint — a hand-typed RTSP camera has neither."""
    return bool(camera.ptz_supported and camera.onvif_xaddr)


def log(ok: bool, label: str, detail: str, status: int) -> None:
    """Make every PTZ hit visible in BOTH places: the app log (journalctl) and
    the monitor's Cloud Sync Console. Rejections included — so "did the app's
    button reach this device, and what did the camera say" is never a guess."""
    (logger.info if ok else logger.warning)("PTZ %s — %s", label or "?", detail)
    call_log.record("ptz", ok, status=status,
                    label=f"{canon(label)} {detail}".strip()[:300])


# ---- home position (what revert() goes back to) ----------------------

def capture_home(camera: Camera, onvif_cam, token: str) -> None:
    """Remember the framing ONCE per override session, BEFORE the camera moves.
    A camera that doesn't answer GetStatus simply never gets a home, and revert()
    then reports "nothing to revert" instead of inventing a position."""
    with _lock:
        if camera.camera_id in _home:
            return
    pos = onvif_cam.ptz_status(token)        # None on unsupported cameras
    if pos is None:
        return
    with _lock:
        _home.setdefault(camera.camera_id, pos)


def has_home(camera_id: str) -> bool:
    """True while this camera is off its original framing (i.e. reverting would
    actually do something)."""
    with _lock:
        return camera_id in _home


def forget_home(camera_id: str) -> None:
    """Drop the captured framing without moving — for a camera being deleted or
    re-added, so a stale position can never be driven to later."""
    with _lock:
        _home.pop(camera_id, None)


# ---- the auto-stop safety net ----------------------------------------

def _cancel_auto_stop(camera_id: str) -> None:
    with _lock:
        timer = _stop_timers.pop(camera_id, None)
    if timer is not None:
        timer.cancel()


def _arm_auto_stop(camera: Camera, onvif_cam, token: str, ms: int) -> int:
    """(Re)arm the single pending auto-stop for this camera — a newer move always
    supersedes the older one. Returns the clamped duration actually used."""
    from onvif.soap import OnvifError
    ms = max(1, min(int(ms), int(settings.ptz_max_move_ms)))

    def _fire() -> None:
        try:
            onvif_cam.ptz_stop(token)
        except OnvifError:
            pass                              # the next command re-establishes control
        with _lock:
            _stop_timers.pop(camera.camera_id, None)

    _cancel_auto_stop(camera.camera_id)
    timer = threading.Timer(ms / 1000.0, _fire)
    timer.daemon = True
    with _lock:
        _stop_timers[camera.camera_id] = timer
    timer.start()
    return ms


# ---- the three operations --------------------------------------------

def move(camera: Camera, pan: float, tilt: float, zoom: float = 0.0,
         duration_ms: int | None = 0) -> int | None:
    """Start a continuous move.

    duration_ms = 0/int : the EDGE owns the stop — auto-stop after that many ms,
                          clamped to ptz_max_move_ms (0 = use the ceiling).
    duration_ms = None  : the CALLER owns the stop (the LAN pad's button
                          release); no timer is armed.

    Returns the ms armed, or None when the caller owns the stop. Raises
    OnvifError when the camera refuses the move."""
    onvif_cam, token = client(camera), token_for(camera)
    capture_home(camera, onvif_cam, token)   # BEFORE the camera leaves its framing
    onvif_cam.ptz_move(token, pan, tilt, zoom)
    used = None if duration_ms is None else _arm_auto_stop(
        camera, onvif_cam, token, duration_ms or settings.ptz_max_move_ms)
    return used


def revert(camera: Camera) -> bool:
    """Drive back to the framing captured before the override, and forget it.

    Returns False when there was nothing to revert (never moved this session, or
    the camera doesn't report its position) — that is a normal, idempotent
    outcome, not an error. Raises OnvifError if the camera rejects AbsoluteMove."""
    _cancel_auto_stop(camera.camera_id)      # a move in flight would fight the revert
    with _lock:
        home = _home.pop(camera.camera_id, None)
    if home is None:
        return False
    try:
        client(camera).ptz_absolute_move(token_for(camera), *home)
    except Exception:
        with _lock:                          # put it back: still off-framing
            _home.setdefault(camera.camera_id, home)
        raise
    return True


def stop(camera: Camera) -> None:
    """Halt now. Used by the LAN installer pad, which owns its own stop; the
    cloud API has none because its moves stop themselves."""
    _cancel_auto_stop(camera.camera_id)
    client(camera).ptz_stop(token_for(camera))
