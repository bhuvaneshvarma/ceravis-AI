from __future__ import annotations

"""
Playback API for the person-triggered recordings.

The recordings ARE the playback segments. MediaMTX writes self-contained MPEG-TS
segments named with their own start time; media.recording_index reads that
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

Everything honours the same control token + edge-id guard as the PTZ endpoint,
so it's safe through the frp tunnel. The {camera} slot accepts EITHER the
camera_id OR the same CameraName label PTZ takes (KITCHEN, LIVING_ROOM, …) — one
addressing rule everywhere. A timestamp is read exactly as sent; a naive value is
edge-local time (common.clock).
"""

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response

from api.control_auth import check_control_token, check_edge_id
from common import clock
from config.settings import settings
from configuration.camera_config import CameraConfig
from media import recording_index
from media.mediamtx_client import record_path_name


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
def recording_timeline(camera: str, edge_id: str | None = None,
                       x_ceravis_control_token: str | None = Header(default=None)):
    """The availability data for the scrub bar: which stretches of the last
    `retention_hours` actually have footage (i.e. someone was present) — read
    straight off the stored segments. The frontend draws a bar from
    window_start→now and shades these `segments`; a click on a shaded point
    becomes a /playback.m3u8?ts=… call. Re-fetch anytime to reflect the rolling
    window (oldest aged out, newest added)."""
    check_control_token(x_ceravis_control_token)
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
def recording_playlist(camera: str, ts: str | None = None,
                       edge_id: str | None = None,
                       x_ceravis_control_token: str | None = Header(default=None)):
    """SEEKABLE playback: an HLS-VOD playlist listing the recorded segments from
    `ts` onward. The segments are the files already on disk — nothing is built,
    so this answers instantly. The player fetches them itself and gets pause +
    scrub + seek for free. No `ts` = this camera's most recent stretch."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    rec_path = record_path_name(cam)
    if ts and ts.strip():
        try:
            since = clock.to_aware(ts)
        except ValueError:
            raise HTTPException(400, f"bad timestamp: {ts!r} (want ISO-8601)")
    else:
        runs = recording_index.ranges(rec_path)
        if not runs:
            raise HTTPException(404, "no footage recorded for this camera yet")
        since = runs[-1][0]
    body = recording_index.playlist(rec_path, since)
    if not body:
        raise HTTPException(404, "no footage at or after that time")
    # Relative segment URIs resolve against this playlist's directory, i.e.
    # /api/v1/recordings/{camera}/segment/<file> — same tunnel, same auth.
    return Response(content=body, media_type="application/vnd.apple.mpegurl")


@router.get("/{camera}/segment/{filename}")
def recording_segment(camera: str, filename: str, edge_id: str | None = None,
                      x_ceravis_control_token: str | None = Header(default=None)):
    """One recorded segment, served straight from disk exactly as recorded."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    path = recording_index.segment_file(record_path_name(cam), filename)
    if path is None:
        raise HTTPException(404, "segment not found (expired or bad name)")
    return FileResponse(str(path), media_type="video/mp2t")
