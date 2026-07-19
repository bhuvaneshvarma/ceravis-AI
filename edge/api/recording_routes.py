from __future__ import annotations

"""
Playback API for the person-triggered recordings.

The frontend talks only to this device API (over the frp tunnel), never to
MediaMTX directly. Two calls drive the whole review UI:

    GET /api/v1/recordings/{camera}/timeline      -> where footage exists over the
                                                     12h window (draw the scrub bar)
    GET /api/v1/recordings/{camera}/playback.m3u8 -> a SEEKABLE HLS-VOD link
        ?ts=<ISO8601>                                (pause/scrub/seek); 307-redirects
                                                     to the generated playlist
    GET /api/v1/recordings/hls/{key}/{file}       -> the playlist + .ts segments

Plus the monitor's recording on/off switch:

    GET  /api/v1/recordings/state
    POST /api/v1/recordings/toggle

timeline + playback honour the same control token + edge-id guard as the PTZ
endpoint, so they're safe through the tunnel. The {camera} slot accepts EITHER
the camera_id OR the same CameraName label PTZ takes (KITCHEN, LIVING_ROOM, …) —
one addressing rule everywhere, resolved by CameraConfig.get_by_label. A
timestamp is read exactly as sent; a naive value is edge-local time (common.clock).
"""

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from api.control_auth import check_control_token, check_edge_id
from common import clock
from config.settings import settings
from configuration.camera_config import CameraConfig
from media import mediamtx_client
from media.hls_playback import HlsError, manager as hls_manager
from media.mediamtx_client import MediaMTXError, record_path_name


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

def _parsed_ranges(cam):
    """[(start_dt, end_dt), …] of this camera's recorded stretches, chronological."""
    ranges = mediamtx_client.list_recordings(record_path_name(cam))
    out = []
    for r in ranges:
        try:
            s = clock.to_aware(r["start"])
            e = s + timedelta(seconds=float(r.get("duration") or 0))
        except (ValueError, KeyError, TypeError):
            continue
        out.append((s, e))
    out.sort(key=lambda se: se[0])
    return out


@router.get("/{camera}/timeline")
def recording_timeline(camera: str, edge_id: str | None = None,
                       x_ceravis_control_token: str | None = Header(default=None)):
    """The availability data for the scrub bar: which stretches of the last
    `retention_hours` actually have footage (i.e. someone was present). The
    frontend draws a bar from window_start→now and shades these `segments`; a
    click on a shaded point becomes a /playback.m3u8?ts=… call. Re-fetch anytime
    to reflect the rolling window (oldest aged out, newest added)."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    try:
        ranges = _parsed_ranges(cam)
    except MediaMTXError as exc:
        raise HTTPException(503, f"recordings unavailable: {exc}")
    now = clock.now()
    window_start = now - timedelta(hours=settings.record_retention_hours)
    segments = []
    for s, e in ranges:
        if e <= window_start:
            continue
        s = max(s, window_start)
        segments.append({"start": s.isoformat(), "end": e.isoformat(),
                         "seconds": round((e - s).total_seconds(), 1)})
    return {
        "camera_id": cam.camera_id,
        "now": now.isoformat(),
        "window_start": window_start.isoformat(),
        "retention_hours": settings.record_retention_hours,
        "recorded_seconds": round(sum(x["seconds"] for x in segments), 1),
        "segments": segments,
    }


# ---- HLS-VOD playback (the seekable "play from this time" link) -----

def _newest_range_start(cam) -> str:
    """Start of this camera's most recent recorded stretch — the default point
    for a /playback with no ts ('show me the latest')."""
    try:
        ranges = _parsed_ranges(cam)
    except MediaMTXError as exc:
        raise HTTPException(404, f"no footage: {exc}")
    if not ranges:
        raise HTTPException(404, "no footage recorded for this camera yet")
    return ranges[-1][0].isoformat()


@router.get("/{camera}/playback.m3u8")
def recording_playlist(camera: str, ts: str | None = None,
                       duration: int | None = None, edge_id: str | None = None,
                       x_ceravis_control_token: str | None = Header(default=None)):
    """SEEKABLE playback: build (or reuse) an HLS-VOD playlist of the footage
    from `ts` onward and redirect to it. The frontend loads the returned URL in
    an HLS player (native in Safari, hls.js elsewhere) and gets pause + scrub +
    seek for free. No `ts` = this camera's most recent stretch. `duration` caps
    how much footage (gaps skipped) the playlist spans."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    if ts and ts.strip():
        try:
            start_iso = clock.to_rfc3339(ts)
        except ValueError:
            raise HTTPException(400, f"bad timestamp: {ts!r} (want ISO-8601)")
    else:
        start_iso = _newest_range_start(cam)
    cap = int(duration) if duration else settings.hls_footage_cap_secs
    try:
        key = hls_manager.ensure(record_path_name(cam), start_iso, cap)
    except HlsError as exc:
        raise HTTPException(404, f"playback unavailable: {exc}")
    # 307 keeps it a GET; the player uses the redirected URL as the base for the
    # relative segment names, so both go back through the same tunnel.
    return RedirectResponse(f"/api/v1/recordings/hls/{key}/index.m3u8",
                            status_code=307)


@router.get("/hls/{key}/{filename}")
def hls_asset(key: str, filename: str):
    """Serve one playback file — index.m3u8 or a .ts segment. The unguessable
    session key is the capability, so these carry no token (an HLS player can't
    attach one to its own segment fetches)."""
    path = hls_manager.file(key, filename)
    if path is None:
        raise HTTPException(404, "playback segment expired or not found")
    media = ("application/vnd.apple.mpegurl" if filename.endswith(".m3u8")
             else "video/mp2t")
    return FileResponse(str(path), media_type=media)
