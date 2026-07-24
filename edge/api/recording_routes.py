from __future__ import annotations

"""
Playback API for the person-triggered recordings.

The recordings ARE the playback segments. MediaMTX writes self-contained MPEG-TS
segments named with their own start time; recording.index reads that
directory and this API hands the frontend a playlist that points straight at
those files. Nothing is re-cut, re-encoded or duplicated for playback.

    GET /api/v1/recordings/{camera}/timeline      -> where footage exists over the
                                                     retention window (scrub bar)
    GET /api/v1/recordings/{camera}/playback.m3u8 -> HLS-VOD playlist over the
        ?ts=<ISO8601>                                STORED segments (seekable)
    GET /api/v1/recordings/{camera}/segment/{file} -> one stored segment, as-is

Plus the monitor's recording on/off switch:

    GET  /api/v1/recordings/state
    POST /api/v1/recordings/toggle

Each request carries this device's edgeId, verified against the provisioned one
(api/control_auth) — like PTZ, and with NO token. The {camera} slot accepts
EITHER the camera_id OR the same CameraName label PTZ takes (KITCHEN, LIVING_ROOM,
…) — one addressing rule everywhere. A timestamp is read exactly as sent; a naive
value is edge-local time (common.clock).
"""

from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from api.control_auth import check_edge_id
from common import clock
from config.settings import settings
from configuration.camera_config import CameraConfig
from recording import index as recording_index
from livestream.mediamtx_client import record_path_name


router = APIRouter(prefix="/api/v1/recordings", tags=["Recordings"])


def _controller(request: Request):
    rc = getattr(request.app.state, "recording_controller", None)
    if rc is None:
        raise HTTPException(503, "recording not available (MediaMTX not running)")
    return rc


def _resolve_camera(camera: str):
    """camera_id first (exact), then the PTZ-style label (KITCHEN, …)."""
    cfg = CameraConfig()
    cam = cfg.get_by_id(camera) or cfg.get_by_label(camera)
    if cam is None:
        raise HTTPException(404, f"no camera for '{camera}'")
    return cam


# ---- recording on/off (monitor button) ------------------------------

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


# ---- timeline (availability for the scrub bar) ----------------------

@router.get("/{camera}/timeline")
def recording_timeline(camera: str, edge_id: str | None = None):
    """The availability data for the scrub bar: which stretches of the last
    `retention_hours` actually have footage (i.e. someone was present) — read
    straight off the stored segments. The frontend draws a bar from
    window_start→now and shades these `segments`; a click on a shaded point
    becomes a /playback.m3u8?ts=… call. Re-fetch anytime to reflect the rolling
    window (oldest aged out, newest added)."""
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    now = clock.now()
    window_start = now - timedelta(hours=settings.record_retention_hours)
    out = []
    for start, end in recording_index.ranges(record_path_name(cam)):
        if end <= window_start:
            continue
        start = max(start, window_start)
        out.append({"start": start.isoformat(), "end": end.isoformat(),
                    "seconds": round((end - start).total_seconds(), 1)})
    return {
        "camera_id": cam.camera_id,
        "now": now.isoformat(),
        "window_start": window_start.isoformat(),
        "retention_hours": settings.record_retention_hours,
        "recorded_seconds": round(sum(x["seconds"] for x in out), 1),
        "segments": out,
    }


# ---- HLS-VOD playback over the STORED segments ----------------------

@router.get("/{camera}/playback.m3u8")
def recording_playlist(camera: str, edge_id: str | None = None,
                       ts: str | None = None):
    """The WHOLE retention window as ONE seekable HLS playlist: every recorded
    stretch of the last `record_retention_hours`, each tagged with its real
    wall-clock time (EXT-X-PROGRAM-DATE-TIME) and joined across the empty gaps
    (EXT-X-DISCONTINUITY). The client loads this ONCE and seeks to any instant
    by date — no per-moment request. The playlist is left open (no ENDLIST)
    while the camera is still recording, so it also follows live; ENDLIST
    appears once recording has stopped.

    `ts` (RecordingPlaybackRequest) is the OPTIONAL alert/snapshot instant to
    jump to — the whole window is served regardless and the client seeks to it
    (the player knows every segment's wall-clock time), so this one URL stays the
    single, cacheable source of a camera's timeline."""
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    window_start = clock.now() - timedelta(hours=settings.record_retention_hours)
    # Carry the edge_id onto every segment URI so each segment fetch authenticates
    # too (works for hls.js AND native HLS, which can't attach headers).
    suffix = f"?edge_id={quote(edge_id)}" if edge_id else ""
    body = recording_index.playlist(record_path_name(cam), window_start,
                                    uri_suffix=suffix)
    if not body:
        raise HTTPException(404, "no footage recorded for this camera in the "
                                 "retention window yet")
    return Response(content=body, media_type="application/vnd.apple.mpegurl")


@router.get("/{camera}/segment/{filename}")
def recording_segment(camera: str, filename: str, edge_id: str | None = None):
    """One recorded segment, served straight from disk exactly as recorded."""
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    path = recording_index.segment_file(record_path_name(cam), filename)
    if path is None:
        raise HTTPException(404, "segment not found (expired or bad name)")
    return FileResponse(str(path), media_type="video/mp2t")
