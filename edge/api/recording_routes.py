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
    GET /api/v1/recordings/{camera_id}/timeline  -> recorded ranges over the 12h
                                                    window, for the scrub bar
    GET /api/v1/recordings/{camera_id}/playback.m3u8  -> SEEKABLE HLS-VOD link
        ?ts=<ISO8601>                                    (pause/scrub); redirects
                                                         to the generated playlist
    GET /api/v1/recordings/hls/{key}/{file}      -> the playlist + .ts segments

The /at and /window endpoints accept the timestamp exactly as it appears on the
alert/snapshot; a naive value is read as edge-local time (the system's single
clock — see common.clock). They honour the same control token + edge-id guard as
the PTZ endpoint, so they're safe to expose through the frp tunnel.

The {camera} slot accepts EITHER the camera_id OR the same CameraName label the
PTZ endpoint takes (KITCHEN, LIVING_ROOM, …) — one addressing rule for every
cloud-facing endpoint, resolved by CameraConfig.get_by_label.
"""

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

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


@router.get("/{camera}")
def camera_recordings(camera: str):
    cam = _resolve_camera(camera)
    try:
        return mediamtx_client.list_recordings(record_path_name(cam))
    except MediaMTXError as exc:
        raise HTTPException(503, f"recordings unavailable: {exc}")


def _stream_from_cam(cam, start_iso: str, duration: float):
    """Stream one MP4 slice from a resolved camera. `start_iso` is normalized to
    an absolute instant first (naive = edge-local), so the timestamp the frontend
    sends always maps to the right footage regardless of its timezone."""
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
                 f'inline; filename="{cam.camera_id}_{start}.mp4"',
                 "X-Clip-Start": start, "X-Clip-Duration": str(duration)},
    )


def _latest_start(cam, duration: float) -> str:
    """Start instant for 'no timestamp given' — the tail of this camera's NEWEST
    recorded range, so an unqualified /at plays the most recent footage."""
    try:
        ranges = mediamtx_client.list_recordings(record_path_name(cam))
    except MediaMTXError as exc:
        raise HTTPException(404, f"no footage: {exc}")
    newest = None
    for r in ranges:
        try:
            s = clock.to_aware(r["start"])
        except (ValueError, KeyError, TypeError):
            continue
        end = s + timedelta(seconds=float(r.get("duration") or 0))
        if newest is None or end > newest:
            newest = end
    if newest is None:
        raise HTTPException(404, "no footage recorded for this camera yet")
    return (newest - timedelta(seconds=max(1.0, duration))).isoformat()


@router.get("/{camera}/clip")
def recording_clip(camera: str, start: str, duration: float = 15.0,
                   edge_id: str | None = None,
                   x_ceravis_control_token: str | None = Header(default=None)):
    """One recorded slice as a standard MP4 (starts at `start`, ISO-8601)."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    return _stream_from_cam(_resolve_camera(camera), start, duration)


@router.get("/{camera}/at")
def recording_at(camera: str, ts: str | None = None,
                 pre: float = 15.0, duration: float = 15.0,
                 edge_id: str | None = None,
                 x_ceravis_control_token: str | None = Header(default=None)):
    """EVENT -> FOOTAGE. Given the timestamp shown on an alert/snapshot, stream
    the recorded clip that STARTS `pre` seconds before it (default 15s) so the
    caregiver sees the lead-up, not just the aftermath. Continue playback by
    calling again with `ts` advanced by `duration` (`X-Clip-Start` echoes the
    exact instant served). With NO `ts`, streams this camera's most recent
    footage instead."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    if ts and ts.strip():
        try:
            start_iso = clock.shift_iso(ts, -max(0.0, pre))
        except ValueError:
            raise HTTPException(400, f"bad timestamp: {ts!r} (want ISO-8601)")
    else:
        start_iso = _latest_start(cam, duration)
    return _stream_from_cam(cam, start_iso, duration)


@router.get("/{camera}/window")
def recording_window(camera: str, ts: str, edge_id: str | None = None,
                     x_ceravis_control_token: str | None = Header(default=None)):
    """Whether footage exists around an event instant, plus this camera's
    recorded ranges — so the player knows if the 'play footage' button has
    anything to show and how far the continuation can run before it runs out."""
    check_control_token(x_ceravis_control_token)
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
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
    return {"camera_id": cam.camera_id, "instant": instant.isoformat(),
            "covered": covered, "ranges": ranges}


# =====================================================================
# TIMELINE + HLS-VOD PLAYBACK (the scrubbable "play from this time" link)
# =====================================================================

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
