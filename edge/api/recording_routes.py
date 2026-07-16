from __future__ import annotations

"""
Playback API for the person-triggered recordings.

Thin proxy over MediaMTX's localhost playback server, so the ceravishealth
frontend talks only to this device API:

    GET /api/v1/recordings                      -> per-camera recorded ranges
    GET /api/v1/recordings/{camera_id}          -> one camera's recorded ranges
    GET /api/v1/recordings/{camera_id}/clip     -> playable MP4 slice
        ?start=<ISO8601>&duration=<secs>
    GET /api/v1/recordings/{camera_id}/at        -> EVENT -> FOOTAGE: the clip
        ?ts=<ISO8601>&pre=15&duration=15            starting `pre` secs BEFORE
                                                    an alert/snapshot timestamp
    GET /api/v1/recordings/{camera_id}/window    -> is there footage around ts?
        ?ts=<ISO8601>

The /at and /window endpoints accept the timestamp exactly as it appears on the
alert/snapshot; a naive value is read as edge-local time (the system's single
clock — see common.clock). They honour the same control token + edge-id guard as
the PTZ endpoint, so they're safe to expose through the frp tunnel.
"""

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.control_auth import check_control_token, check_edge_id
from common import clock
from configuration.camera_config import CameraConfig
from media import mediamtx_client
from media.mediamtx_client import MediaMTXError, record_path_name


router = APIRouter(prefix="/api/v1/recordings", tags=["Recordings"])


def _controller(request: Request):
    rc = getattr(request.app.state, "recording_controller", None)
    if rc is None:
        raise HTTPException(503, "recording not available (MediaMTX not running)")
    return rc


@router.get("/state")
def recording_state(request: Request):
    """Runtime recording switch + which cameras are recording right now."""
    rc = getattr(request.app.state, "recording_controller", None)
    if rc is None:
        return {"available": False, "enabled": False, "recording": []}
    return {"available": True, "enabled": rc.is_enabled(),
            "recording": rc.recording_now()}


@router.post("/toggle")
def recording_toggle(request: Request):
    """Flip person-triggered recording on/off (persisted; OFF also stops any
    active recordings immediately — nothing is saved until turned back on)."""
    rc = _controller(request)
    return {"enabled": rc.set_enabled(not rc.is_enabled())}


@router.get("")
def all_recordings():
    """Recorded time-ranges for every camera, newest camera list first."""
    out = {}
    for cam in CameraConfig().get_all():
        try:
            out[cam.camera_id] = mediamtx_client.list_recordings(
                record_path_name(cam))
        except MediaMTXError:
            out[cam.camera_id] = []
    return out


@router.get("/{camera_id}")
def camera_recordings(camera_id: str):
    cam = CameraConfig().get_by_id(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    try:
        return mediamtx_client.list_recordings(record_path_name(cam))
    except MediaMTXError as exc:
        raise HTTPException(503, f"recordings unavailable: {exc}")


def _stream_clip(camera_id: str, start_iso: str, duration: float):
    """Shared MP4-slice responder for /clip and /at. `start_iso` is normalized to
    an absolute instant first (naive = edge-local), so the timestamp the frontend
    sends always maps to the right footage regardless of its timezone."""
    cam = CameraConfig().get_by_id(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    try:
        start = clock.to_rfc3339(start_iso)
    except ValueError:
        raise HTTPException(400, f"bad timestamp: {start_iso!r} (want ISO-8601)")
    try:
        upstream = mediamtx_client.open_clip(record_path_name(cam), start, duration)
    except MediaMTXError as exc:
        raise HTTPException(404, f"no footage at that time: {exc}")
    return StreamingResponse(
        upstream.iter_content(chunk_size=64 * 1024),
        media_type="video/mp4",
        headers={"Content-Disposition":
                 f'inline; filename="{camera_id}_{start}.mp4"',
                 "X-Clip-Start": start, "X-Clip-Duration": str(duration)},
    )


@router.get("/{camera_id}/clip")
def recording_clip(camera_id: str, start: str, duration: float = 15.0,
                   edge_id: str | None = None,
                   x_ceravis_control_token: str | None = Header(default=None)):
    """One recorded slice as a standard MP4 (starts at `start`, ISO-8601)."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    return _stream_clip(camera_id, start, duration)


@router.get("/{camera_id}/at")
def recording_at(camera_id: str, ts: str, pre: float = 15.0, duration: float = 15.0,
                 edge_id: str | None = None,
                 x_ceravis_control_token: str | None = Header(default=None)):
    """EVENT -> FOOTAGE. Given the timestamp shown on an alert/snapshot, stream
    the recorded clip that STARTS `pre` seconds before it (default 15s) so the
    caregiver sees the lead-up to the event, not just the aftermath. The frontend
    continues playback by calling again with `ts` advanced by `duration`
    (`X-Clip-Start` in the response echoes the exact instant served)."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    try:
        start_iso = clock.shift_iso(ts, -max(0.0, pre))
    except ValueError:
        raise HTTPException(400, f"bad timestamp: {ts!r} (want ISO-8601)")
    return _stream_clip(camera_id, start_iso, duration)


@router.get("/{camera_id}/window")
def recording_window(camera_id: str, ts: str, edge_id: str | None = None,
                     x_ceravis_control_token: str | None = Header(default=None)):
    """Whether footage exists around an event instant, plus this camera's
    recorded ranges — so the player knows if the 'play footage' button has
    anything to show and how far the continuation can run before it runs out."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = CameraConfig().get_by_id(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    try:
        instant = clock.to_aware(ts)
    except ValueError:
        raise HTTPException(400, f"bad timestamp: {ts!r} (want ISO-8601)")
    try:
        ranges = mediamtx_client.list_recordings(record_path_name(cam))
    except MediaMTXError as exc:
        raise HTTPException(503, f"recordings unavailable: {exc}")
    covered = False
    for r in ranges:
        try:
            s = clock.to_aware(r["start"])
            e = s + timedelta(seconds=float(r.get("duration") or 0))
        except (ValueError, KeyError, TypeError):
            continue
        if s <= instant <= e:
            covered = True
            break
    return {"camera_id": camera_id, "instant": instant.isoformat(),
            "covered": covered, "ranges": ranges}
