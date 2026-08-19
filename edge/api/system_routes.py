from __future__ import annotations

"""
System endpoints: root redirect, health and the live status surface.

This process serves NO video. Every viewer — the built-in UI pages, the cloud,
a phone — plays the camera's compressed stream straight from MediaMTX over
WebRTC. Kept here so main.py is just app wiring.
"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from common import clock
from config.settings import settings
from configuration.camera_config import CameraConfig
from ingestion.camera_status import codec_warning, substream_warning


router = APIRouter()

_EDGE_ROOT = Path(__file__).resolve().parents[1]


@router.get("/")
def root():
    return RedirectResponse("/ui/live.html")


@router.get("/health")
async def health():
    from livestream.mediamtx_client import is_up
    return {"status": "ok", "version": settings.app_version,
            "device_id": settings.device_id,
            # False = live links/recording are dead even though the app is up
            # (mediamtx missing or crashed — see data/mediamtx.log).
            "media_backbone": is_up()}


@router.get("/api/v1/cloud/activity")
def cloud_activity(request: Request, limit: int = 100):
    """Rolling log of app-server calls (saveAlert/saveSnapshot/…) — feeds the
    monitor's Cloud Sync Console so end-to-end tests are verifiable on screen.

    `outbox` rides along on the same poll: how many uploads are queued and how
    old the oldest is. That is what turns "the cloud is quiet" into either
    "nothing happened" or "we are offline and holding N incidents"."""
    from integration.call_log import recent
    ob = getattr(request.app.state, "outbox", None)
    return {"calls": recent(limit=min(int(limit), 300)),
            "outbox": ob.stats() if ob is not None else None}


@router.get("/api/v1/cloud/outbox")
def cloud_outbox(request: Request, limit: int = 50):
    """The upload queue itself — every job with its state, attempts and last
    error, newest first. The detail level behind the console's queue counter,
    for answering "what exactly is stuck, and why"."""
    ob = getattr(request.app.state, "outbox", None)
    if ob is None:
        return {"available": False, "jobs": []}
    return {"available": True, **ob.stats(),
            "jobs": ob.recent(limit=min(int(limit), 300))}


# =====================================================================
# LIVE SYSTEM STATUS — the one health surface for the ceravis-status CLI
# and the monitor. Everything here is best-effort so it also answers on a
# dev box (no systemd, no MediaMTX binary): missing pieces report null,
# never raise.
# =====================================================================

def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, clock.local_tz()).isoformat() if epoch else None


def _time_status() -> dict:
    """Edge-local time + whether the OS clock is NTP-disciplined. The whole
    system stamps events, snapshots and recordings on THIS clock, so a drifted
    or unsynced clock silently misaligns alerts with their footage."""
    info = {"local": clock.now_iso(), "timezone": str(clock.local_tz()),
            "ntp_synchronized": None}
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "-p", "Timezone",
             "--value"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            vals = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            if vals:
                info["ntp_synchronized"] = vals[0].lower() == "yes"
            if len(vals) > 1:
                info["timezone"] = vals[1]
    except (OSError, subprocess.SubprocessError):
        pass                                    # not systemd (dev box) — leave null
    return info


def _storage_status() -> dict:
    """Disk headroom on the recordings volume + the actual footage footprint and
    its time span (should never exceed the retention window)."""
    rec = Path(settings.record_dir)
    rec = rec if rec.is_absolute() else (_EDGE_ROOT / rec)
    out = {"path": str(rec), "retention_hours": settings.record_retention_hours}
    probe = rec
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        total, used, free = shutil.disk_usage(str(probe))
        out.update(disk_total_gb=round(total / 1e9, 1),
                   disk_used_gb=round(used / 1e9, 1),
                   disk_free_gb=round(free / 1e9, 1),
                   disk_used_pct=round(used / total * 100, 1) if total else None)
    except OSError:
        pass
    total_bytes = 0
    oldest = newest = None
    if rec.exists():
        for f in rec.rglob("*"):
            if f.suffix.lower() not in (".mp4", ".ts"):
                continue
            try:
                s = f.stat()
            except OSError:
                continue
            total_bytes += s.st_size
            oldest = s.st_mtime if oldest is None else min(oldest, s.st_mtime)
            newest = s.st_mtime if newest is None else max(newest, s.st_mtime)
    out["recordings_gb"] = round(total_bytes / 1e9, 2)
    out["oldest_segment"] = _iso(oldest)
    out["newest_segment"] = _iso(newest)
    return out


def _camera_status(manager=None) -> list[dict]:
    """Per-camera: is MediaMTX actually serving its path, what codec and
    RESOLUTION are live, how many consumers are reading it, PTZ capability.

    `resolution` is measured off the decoded frames, not read from config. A
    camera accidentally pointed at its sub-stream is healthy by every other
    measure — running, good fps, no reconnects — while feeding the AI, the
    recordings and every live link a fraction of the pixels. Reporting the real
    number is the only thing that makes that visible."""
    from livestream.mediamtx_client import path_info, path_stream_info
    live = manager.get_status() if manager is not None else {}
    cams = []
    for cam in CameraConfig().get_all():
        info = path_info(cam.camera_id) or {}
        st = live.get(cam.camera_id)
        w = getattr(st, "frame_width", 0) or 0
        h = getattr(st, "frame_height", 0) or 0
        # Read off the wire, so codec/profile are evidence rather than a label.
        wire = path_stream_info(cam.camera_id) or {}
        cams.append({
            "camera_id": cam.camera_id,
            "name": getattr(cam, "camera_name", ""),
            "room": getattr(cam, "room_name", ""),
            "enabled": bool(getattr(cam, "is_enabled", True)),
            "path_ready": bool(info.get("ready")),
            "codec": wire.get("codec"),
            # H.264 has profiles a decoder can refuse; ONVIF claimed "Main" on a
            # stream ffprobe read as "High", so report what is really there.
            "profile": wire.get("profile"),
            "resolution": f"{w}x{h}" if w and h else None,
            "width": w,
            "height": h,
            "readers": len(info.get("readers") or []),
            "ptz": bool(getattr(cam, "ptz_supported", False)),
        })
    return cams


def _cloud_status(outbox=None) -> dict:
    try:
        from integration.call_log import recent
        from integration.ceravis_api import is_configured
        calls = recent(50)
        return {
            "configured": is_configured(),
            "recent_calls": len(calls),
            "recent_errors": sum(1 for c in calls if not c.get("ok", True)),
            "last_call": calls[0] if calls else None,
            # The upload queue: depth 0 means everything raised has been
            # delivered. Anything else is a backlog with an age.
            "outbox": outbox.stats() if outbox is not None else None,
        }
    except Exception:
        return {"configured": False}


@router.get("/api/v1/system/status")
def system_status(request: Request):
    """Full live status for the ceravis-status CLI and the monitor. `status` is
    'degraded' when any safety-relevant subsystem is unhealthy — the backbone is
    down, a camera that should be recording isn't landing segments, the disk is
    nearly full, or the clock isn't NTP-synced — with the specific reasons in
    `problems`."""
    from livestream.mediamtx_client import is_up
    st = request.app.state
    backbone_up = is_up()
    rc = getattr(st, "recording_controller", None)
    recording = {
        "available": rc is not None,
        "enabled": rc.is_enabled() if rc else False,
        "recording_now": rc.recording_now() if rc else [],
        "cameras": rc.health() if rc else [],
    }
    storage = _storage_status()
    time_info = _time_status()
    cameras = _camera_status(getattr(st, "camera_manager", None))

    problems: list[str] = []
    if not backbone_up:
        problems.append("media backbone down (no live links, no recording)")
    for c in cameras:
        if not c["enabled"]:
            continue
        for warning in (substream_warning(c["camera_id"], c.get("width") or 0,
                                          c.get("height") or 0),
                        codec_warning(c["camera_id"], c.get("codec"))):
            if warning:
                problems.append(warning)
    for c in recording["cameras"]:
        if not c.get("writing_ok", True):
            problems.append(f"{c['camera_id']}: recording but no segments landing")
    if (storage.get("disk_used_pct") or 0) >= 90:
        problems.append(f"recordings disk {storage.get('disk_used_pct')}% full")
    if time_info.get("ntp_synchronized") is False:
        problems.append("system clock is not NTP-synced")
    cloud = _cloud_status(getattr(st, "outbox", None))
    queue = cloud.get("outbox") or {}
    stuck = queue.get("oldest_pending_age_secs") or 0
    # A brief blip drains itself and is not worth reporting; an upload that has
    # been waiting a quarter of an hour means the link to the cloud is really
    # down, and the operator should hear it from the device, not from a missing
    # alert on their phone.
    if stuck >= 900:
        problems.append(
            f"cloud uploads backing up — {queue.get('pending')} waiting, oldest "
            f"{stuck / 60:.0f} min old ({queue.get('last_error') or 'no response'})")

    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "version": settings.app_version,
        "device_id": settings.device_id,
        "edge_id": settings.edge_id,
        "time": time_info,
        "media_backbone": {"up": backbone_up, "binary": settings.mediamtx_binary},
        "cameras": cameras,
        "recording": recording,
        "storage": storage,
        "cloud": cloud,
    }
