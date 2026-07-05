from __future__ import annotations

"""
Playback API for the person-triggered recordings.

Thin proxy over MediaMTX's localhost playback server, so the ceravishealth
frontend talks only to this device API:

    GET /api/v1/recordings                      -> per-camera recorded ranges
    GET /api/v1/recordings/{camera_id}          -> one camera's recorded ranges
    GET /api/v1/recordings/{camera_id}/clip     -> playable MP4 slice
        ?start=<ISO8601>&duration=<secs>
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

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


@router.get("/{camera_id}/clip")
def recording_clip(camera_id: str, start: str, duration: float = 15.0):
    """One recorded slice as a standard MP4 (starts at `start`, ISO8601 UTC)."""
    cam = CameraConfig().get_by_id(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    try:
        upstream = mediamtx_client.open_clip(record_path_name(cam), start, duration)
    except MediaMTXError as exc:
        raise HTTPException(404, f"clip unavailable: {exc}")
    return StreamingResponse(
        upstream.iter_content(chunk_size=64 * 1024),
        media_type="video/mp4",
        headers={"Content-Disposition":
                 f'inline; filename="{camera_id}_{start}.mp4"'},
    )
