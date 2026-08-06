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
    GET /api/v1/recordings/{camera}/snapshot      -> ONE still (base64 JSON, or
        ?ts=<ISO8601>&quality=&format=jpeg           ?format=jpeg) — live now, or
                                                     a frame-accurate still at a
                                                     past instant (photo twin of
                                                     the playlist)
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

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from api.control_auth import check_edge_id
from common import clock
from config.settings import settings
from configuration.camera_config import CameraConfig
from recording import index as recording_index
from recording import snapshot as recording_snapshot
from recording.snapshot import SnapshotError
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
    # Segment URIs are RELATIVE (`segment/<file>?edge_id=…`) — the HLS standard.
    # The player resolves them against the playlist's OWN url, so if the client
    # loads playback.m3u8 from the per-edge url (…/<edge_id>/api/…/playback.m3u8)
    # the clips inherit the /<edge_id>/api/… prefix automatically. We do NOT emit
    # an absolute/edge-prefixed path here: a client that rebuilds segment urls by
    # prepending its own base would then double the path.
    body = recording_index.playlist(record_path_name(cam), window_start,
                                    uri_suffix=suffix)
    if not body:
        raise HTTPException(404, "no footage recorded for this camera in the "
                                 "retention window yet")
    return Response(content=body, media_type="application/vnd.apple.mpegurl")


# ---- still-frame snapshot (the photo twin of playback) --------------

def _parse_ts(ts: str) -> datetime:
    """Parse the requested instant, tolerant of the shapes a mobile client
    actually sends. Returns an aware datetime.

    Accepts:
      • ISO-8601 with an offset      2026-08-05T12:15:30+05:30
      • ISO-8601 with a 'Z' (UTC)    2026-08-05T12:15:30Z   (pre-3.11
                                     fromisoformat rejects 'Z' — normalized here)
      • ISO-8601 naive (no offset)   2026-08-05T12:15:30   -> read as edge-local
      • Unix epoch seconds or millis 1754397330 / 1754397330000
    A naive value is edge-local (common.clock) — the same rule the playlist uses,
    so a bare wall-clock read off the timeline lands on the frame it means."""
    ts = (ts or "").strip()
    if not ts:
        raise HTTPException(400, "ts is empty")
    # Unix epoch (seconds or milliseconds).
    if ts.lstrip("+-").isdigit():
        n = int(ts)
        secs = n / 1000.0 if abs(n) >= 1_000_000_000_000 else float(n)
        return datetime.fromtimestamp(secs, tz=timezone.utc).astimezone()
    # ISO-8601. Normalize a trailing Z (and a space used instead of 'T').
    iso = ts[:-1] + "+00:00" if ts[-1] in "Zz" else ts
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise HTTPException(
            400, "bad ts — send ISO-8601 like 2026-08-05T12:15:30+05:30 "
                 f"(a trailing Z or a Unix epoch is fine too): {ts!r}")
    return dt if dt.tzinfo else dt.astimezone()


@router.get("/{camera}/snapshot")
def recording_snapshot_endpoint(camera: str, request: Request,
                                edge_id: str | None = None,
                                ts: str | None = None, quality: int = 80,
                                format: str = "base64"):
    """A single still of one camera, for the mobile / cloud live view that can't
    grab a frame from its WebRTC player.

        GET /api/v1/recordings/{camera}/snapshot?edge_id=<id>          -> now
        GET /api/v1/recordings/{camera}/snapshot?edge_id=<id>&ts=<ISO> -> that instant

    No `ts` (or a `ts` at ~now): the freshest LIVE frame — buffered when fresh
    (instant), else a one-shot grab off the backbone. A past `ts`: a frame-
    accurate still decoded from the recorded segment covering it — the returned
    `timestamp` IS the requested one (exact match within the retention window;
    404 where no footage exists).

    `format` (default "base64") -> a JSON body with the image inline as a
    data-URI (what the mobile app consumes); `format=jpeg` -> the raw JPEG bytes
    with the same facts in X-Snapshot-* headers (lighter, for <img>/players).
    Either way the instant the frame represents is on the SAME device clock the
    camera OSD is disciplined to, so the reported time matches the pixels.

    Addressing (camera_id OR the PTZ-style label) and auth (the edgeId match) are
    identical to playback and PTZ — one rule everywhere."""
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    quality = max(1, min(100, int(quality)))

    want = _parse_ts(ts) if ts else None
    now = clock.now()
    live = want is None or want >= now - timedelta(seconds=settings.frame_stale_secs)

    def _live():
        return recording_snapshot.live_snapshot(
            cam, getattr(request.app.state, "camera_manager", None)
            and request.app.state.camera_manager.frame_buffer,
            mediamtx_active=getattr(request.app.state, "mediamtx_active", False),
            quality=quality)

    try:
        if live:
            snap = _live()
        else:
            try:
                snap = recording_snapshot.archive_snapshot(cam, want, quality=quality)
            except SnapshotError:
                # A very RECENT instant may have no FINALIZED segment yet (its
                # covering segment is still being written), so a just-fired event
                # would 404. Within that window fall back to the freshest live
                # frame so the app still gets a picture; an OLDER instant with no
                # footage stays a real 404 (nobody present / past retention).
                if (now - want) > timedelta(seconds=2 * settings.record_segment_secs):
                    raise
                snap = _live()
                live = True                    # headers + error-mapping now reflect live
    except SnapshotError as exc:
        # No footage at a past instant is a genuine 404; a live-capture failure
        # (camera unreachable right now) is a 503 the caller can retry.
        raise HTTPException(404 if not live else 503, str(exc))

    delta_ms = (None if want is None
                else round(abs((snap.time - want).total_seconds()) * 1000))

    if format.lower() == "jpeg":
        headers = {"X-Snapshot-Time": snap.time.isoformat(),
                   "X-Snapshot-Source": snap.source,
                   "Cache-Control": "no-store"}
        if want is not None:
            headers["X-Requested-Time"] = want.isoformat()
            headers["X-Snapshot-Delta-Ms"] = str(delta_ms)
        return Response(content=snap.jpeg, media_type="image/jpeg", headers=headers)

    # Default: JSON with the image as a base64 data-URI. `timestamp` is the
    # instant the frame represents; for a past `ts` it equals `requestedTimestamp`
    # exactly (deltaMs 0) — the snapshot and the request line up.
    b64 = base64.b64encode(snap.jpeg).decode("ascii")
    return JSONResponse({
        "cameraId": cam.camera_id,
        "label": cam.camera_name,
        "source": snap.source,
        "timestamp": snap.time.isoformat(),
        "requestedTimestamp": want.isoformat() if want is not None else None,
        "deltaMs": delta_ms,
        "format": "jpeg",
        "width": snap.width,
        "height": snap.height,
        "bytes": len(snap.jpeg),
        "image": f"data:image/jpeg;base64,{b64}",
    }, headers={"Cache-Control": "no-store"})


@router.get("/{camera}/segment/{filename}")
def recording_segment(camera: str, filename: str, edge_id: str | None = None):
    """One recorded segment, served straight from disk exactly as recorded."""
    check_edge_id(edge_id)
    cam = _resolve_camera(camera)
    path = recording_index.segment_file(record_path_name(cam), filename)
    if path is None:
        raise HTTPException(404, "segment not found (expired or bad name)")
    return FileResponse(str(path), media_type="video/mp2t")
