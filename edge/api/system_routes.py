from __future__ import annotations

"""
System endpoints: root redirect, health and the live status surface.

This process serves NO video. Every viewer — the built-in UI pages, the cloud,
a phone — plays the camera's compressed stream straight from MediaMTX over
WebRTC. Kept here so main.py is just app wiring.
"""

import json
import shutil
import subprocess
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from common import clock, event_snapshots
from config.settings import APP_DIR, resolve_path, settings
from config.account_config import account_recipient, effective_edge_id
from config.camera_config import CameraConfig
from ingestion import illumination
from ingestion.camera_status import codec_warning, substream_warning
from integration import call_log
from maintenance import reboot


router = APIRouter()



def _git_head() -> dict:
    """The commit this code is: git's HEAD in a development clone; in a
    production release (no .git) its manifest.json, read without a process."""
    root = APP_DIR.parent
    if not (root / ".git").exists():
        try:
            m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            return {"commit": str(m.get("commit", "")), "branch": str(m.get("branch", ""))}
        except (OSError, ValueError):
            return {"commit": "", "branch": ""}

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(APP_DIR), *args], capture_output=True,
                                  text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": git("rev-parse", "--short", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD")}


# The code this process was started from. The UI is served straight from disk, so
# a `git pull` changes the pages at once while the Python keeps running what it
# started with — "pulled but not restarted" is a half-updated device.
_RUNNING = _git_head()
_on_disk: tuple[float, dict] = (0.0, _RUNNING)


def code_version() -> dict:
    """Which code is running, which is on disk, and whether they differ."""
    global _on_disk
    if time.monotonic() - _on_disk[0] > 10:
        _on_disk = (time.monotonic(), _git_head())
    disk = _on_disk[1]
    return {**_RUNNING, "on_disk": disk["commit"],
            "restart_needed": bool(disk["commit"]) and disk["commit"] != _RUNNING["commit"]}


@router.get("/")
def root(request: Request):
    """A device not yet set up opens on its first-run status screen; once a care
    account has signed in, on the live wall (which asks for sign-in itself).
    Through the tunnel the page keeps the /<edge_id> prefix."""
    from api.control_auth import FLEET_EDGE_KEY
    from api.setup_routes import commissioned
    page = "live.html" if commissioned() else "welcome.html"
    eid = request.scope.get(FLEET_EDGE_KEY)
    return RedirectResponse(f"/{eid}/ui/{page}" if eid else f"/ui/{page}")


@router.get("/health")
async def health():
    from livestream.mediamtx_client import is_up
    return {"status": "ok", "version": settings.app_version,
            "code": code_version(),
            "device_id": settings.device_id,
            # False = live links/recording are dead even though the app is up
            # (mediamtx missing or crashed — see data/mediamtx.log).
            "media_backbone": is_up()}


@router.get("/api/v1/cloud/activity")
def cloud_activity(request: Request, limit: int = 100, direction: str | None = None):
    """Rolling log of app-server calls (saveAlert/saveSnapshot/…) — feeds the
    monitor's Cloud Sync Console so end-to-end tests are verifiable on screen.

    `outbox` rides along on the same poll: how many uploads are queued and how
    old the oldest is. That is what turns "the cloud is quiet" into either
    "nothing happened" or "we are offline and holding N incidents"."""
    from integration.call_log import recent
    ob = getattr(request.app.state, "outbox", None)
    return {"calls": recent(limit=min(int(limit), 300),
                            direction=direction if direction in ("in", "out") else None),
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
    # The sync flag has ONE reader, reboot.clock_synchronized(). This used to ask
    # timedatectl for two properties at once and take the first line as the flag,
    # but `--value` prints them in systemd's own order (Timezone first), so a
    # synced clock read as NOT synced (status DEGRADED) and the timezone as "yes".
    return {"local": clock.now_iso(), "timezone": _timezone(),
            "ntp_synchronized": reboot.clock_synchronized()}


_tz_cache: tuple[float, str] | None = None


def _timezone() -> str:
    """The system timezone's name (Asia/Kolkata), asked of timedatectl at most
    once a minute — it changes only when someone sets it, and status is polled."""
    global _tz_cache
    if _tz_cache is not None and time.monotonic() - _tz_cache[0] < 60:
        return _tz_cache[1]
    tz = str(clock.local_tz())                  # not systemd (dev box): the abbreviation
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            tz = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    _tz_cache = (time.monotonic(), tz)
    return tz


# Both footprints below are measured by walking the disk, and this endpoint is
# polled on a timer (the Video Console asks every 15s). Retention makes those
# walks bounded but not small — a fortnight of event stills is thousands of
# files — so the answer is cached briefly. It changes on the scale of minutes;
# stat'ing every file again for each poll would be the one part of the status
# surface that costs real I/O on the device it is reporting on.
_STORAGE_TTL_SECS = 60.0
_storage_cache: tuple[float, dict] | None = None


def _storage_status() -> dict:
    """Disk headroom on the recordings volume + the actual footprint of the two
    things that occupy it — footage and event snapshots — each with the window
    that bounds it. A footprint drifting past its window is the visible symptom
    of a retention sweep that has stopped running.

    Cached for _STORAGE_TTL_SECS; see the note above."""
    global _storage_cache
    now = time.monotonic()
    if _storage_cache is not None and now - _storage_cache[0] < _STORAGE_TTL_SECS:
        return _storage_cache[1]
    out = _measure_storage()
    _storage_cache = (now, out)
    return out


def _measure_storage() -> dict:
    rec = resolve_path(settings.record_dir)
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
    out.update(_event_snapshot_footprint())
    return out


def _event_snapshot_footprint() -> dict:
    """What data/events is costing, and the window it is held to. The stills are
    small individually and written on every event, which is exactly the shape of
    growth nobody notices until the disk the footage needs is full."""
    root = event_snapshots.events_root()
    count = 0
    nbytes = 0
    if root.exists():
        for f in root.rglob("*.jpg"):
            count += 1
            try:
                nbytes += f.stat().st_size
            except OSError:
                pass
    return {"event_snapshots": count,
            "event_snapshots_gb": round(nbytes / 1e9, 2),
            "event_retention_days": settings.event_retention_days}


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
            # Colour or infrared right now, with the measured colour variation
            # and the camera's own IrCutFilter report — what the thresholds in
            # settings.illumination_* are tuned against on site.
            "illumination": illumination.describe(cam.camera_id),
            "width": w,
            "height": h,
            "readers": len(info.get("readers") or []),
            "ptz": bool(getattr(cam, "ptz_supported", False)),
        })
    return cams


def _tunnel_status() -> dict:
    """Is the fleet tunnel ACTUALLY keyed to this device's edge_id. Reported
    because a tunnel that silently lost its routing looks identical to a
    healthy one from here — right up until every live link is dead."""
    try:
        from integration.edge_provision import tunnel_status
        return tunnel_status()
    except Exception:
        return {"keyed": False, "reason": "tunnel status unavailable"}


def _cloud_status(outbox=None) -> dict:
    try:
        from integration.call_log import recent
        from integration.ceravis_api import api_key_problem, is_configured
        calls = recent(50)
        return {
            "configured": is_configured(),
            # No key, or still the old public one — None when the key is fine.
            "api_key_problem": api_key_problem(),
            "recent_calls": len(calls),
            "recent_errors": sum(1 for c in calls if not c.get("ok", True)),
            "last_call": calls[0] if calls else None,
            # The upload queue: depth 0 means everything raised has been
            # delivered. Anything else is a backlog with an age.
            "outbox": outbox.stats() if outbox is not None else None,
        }
    except Exception:
        return {"configured": False}


def _backbone_log() -> list[str]:
    """MediaMTX's last log lines — why the backbone is down. Never raises: a
    status endpoint must answer even when the thing it reports on is broken."""
    try:
        from livestream.mediamtx_supervisor import recent_log
        return recent_log(8)
    except Exception:
        return []


def _gather(request: Request) -> dict:
    """Everything the status and health reports read, measured ONCE per call."""
    from livestream.mediamtx_client import is_up
    st = request.app.state
    rc = getattr(st, "recording_controller", None)
    manager = getattr(st, "camera_manager", None)
    from api.setup_routes import internet_problem
    return {
        "state": st,
        "backbone_up": is_up(),
        "recording": {
            "available": rc is not None,
            "enabled": rc.is_enabled() if rc else False,
            "recording_now": rc.recording_now() if rc else [],
            "cameras": rc.health() if rc else [],
        },
        "storage": _storage_status(),
        "time": _time_status(),
        "cameras": _camera_status(manager),
        "live": manager.get_status() if manager is not None else {},
        "internet": internet_problem(),
        "cloud": _cloud_status(getattr(st, "outbox", None)),
        "code": code_version(),
    }


def _warnings(g: dict) -> list[tuple[str, str | None, str]]:
    """Every warning rule, ONCE: (part, camera_id or None, message), in the order
    /system/status lists them. /system/health groups the same list by part, so
    the two reports can never disagree."""
    out: list[tuple[str, str | None, str]] = []
    if not g["backbone_up"]:
        out.append(("mediamtx", None, "media backbone down (no live links, no recording)"))
    for c in g["cameras"]:
        if not c["enabled"]:
            continue
        for warning in (substream_warning(c["camera_id"], c.get("width") or 0,
                                          c.get("height") or 0),
                        codec_warning(c["camera_id"], c.get("codec"))):
            if warning:
                out.append(("camera", c["camera_id"], warning))
    for c in g["recording"]["cameras"]:
        if not c.get("writing_ok", True):
            out.append(("camera", c["camera_id"],
                        f"{c['camera_id']}: recording but no segments landing"))
    storage = g["storage"]
    if (storage.get("disk_used_pct") or 0) >= 90:
        out.append(("hardware", None, f"recordings disk {storage.get('disk_used_pct')}% full"))
    if g["time"].get("ntp_synchronized") is False:
        out.append(("time", None, "system clock is not NTP-synced"))
    if g["internet"]:
        out.append(("internet", None, g["internet"]))
    st = g["state"]
    monitor = getattr(st, "system_monitor", None)
    if monitor is not None:
        out.extend(("hardware", None, p) for p in monitor.problems())
    # The whole chain below detection (tracking, ReID, pose, falls, no-motion)
    # is gated on an enrolled gallery. It can be empty on purpose — removing the
    # recipient's embeddings is how the AI is switched off today — but either way
    # the device is not watching for falls, and status must say so plainly.
    gallery = getattr(st, "gallery", None)
    if gallery is not None and gallery.size == 0 and account_recipient():
        out.append(("ai", None, "AI chain off — no enrolled embeddings, so tracking, "
                                "fall and no-motion detection are not running"))
    cloud = g["cloud"]
    # Before the upload symptoms below: a missing key is their cause, and with
    # the old built-in key nothing fails YET — only this line says so.
    if cloud.get("api_key_problem"):
        out.append(("app_server", None, cloud["api_key_problem"]))
    queue = cloud.get("outbox") or {}
    stuck = queue.get("oldest_pending_age_secs") or 0
    # A brief blip drains itself and is not worth reporting; an upload that has
    # been waiting a quarter of an hour means the link to the cloud is really
    # down, and the operator should hear it from the device, not from a missing
    # alert on their phone.
    if stuck >= 900:
        out.append(("app_server", None,
                    f"cloud uploads backing up — {queue.get('pending')} waiting, oldest "
                    f"{stuck / 60:.0f} min old ({queue.get('last_error') or 'no response'})"))
    # A rejection code that usually means a human must act (bad API key, wrong
    # patient, clip too big). The upload is NOT lost — it keeps retrying — but
    # the cause wants fixing, so it surfaces as a degraded reason on its own.
    code = g["code"]
    if code["restart_needed"]:
        out.append(("backend", None, f"code on disk ({code['on_disk']}) is not the code running "
                                     f"({code['commit']}) — restart the ceravis service"))
    attn = queue.get("attention")
    if attn:
        out.append(("app_server", None,
                    f"cloud uploads need attention — server rejecting with HTTP "
                    f"{attn.get('code')} ({attn.get('reason') or 'see console'}); still "
                    f"queued and retrying, not lost"))
    return out


@router.get("/api/v1/system/status")
def system_status(request: Request):
    """Full live status for the ceravis-status CLI and the monitor. `status` is
    'degraded' when any safety-relevant subsystem is unhealthy — the backbone is
    down, a camera that should be recording isn't landing segments, the disk is
    nearly full, or the clock isn't NTP-synced — with the specific reasons in
    `problems`."""
    g = _gather(request)
    problems = [message for _, _, message in _warnings(g)]
    backbone_up = g["backbone_up"]
    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "version": settings.app_version,
        "code": g["code"],
        "device_id": settings.device_id,
        # The LIVE routing token, resolved the one canonical way (the edge_id
        # the FMS issued — config.account_config.effective_edge_id).
        "edge_id": effective_edge_id(),
        "time": g["time"],
        # When it is DOWN, say WHY: MediaMTX's own last lines are the cause
        # (a config key this build rejects -> it exits on boot and nothing
        # binds; a port in use; a missing cert). Omitted while healthy.
        "media_backbone": {"up": backbone_up,
                           "binary": settings.mediamtx_binary,
                           **({} if backbone_up else {"log_tail": _backbone_log()})},
        "cameras": g["cameras"],
        "recording": g["recording"],
        "storage": g["storage"],
        "cloud": g["cloud"],
        # Whether frpc is REALLY routing this device's edge_id. A tunnel that
        # lost its routing looks identical to a healthy one from in here — right
        # up until every live link is dead.
        "tunnel": _tunnel_status(),
    }


_STARTED = time.monotonic()                  # this process's start, for its uptime


# One vocabulary for the state of every part (edge/LOGS.md, the heartbeat):
OPERATIONAL, DEGRADED, OUTAGE, DISABLED, UNKNOWN = (
    "operational", "degraded", "outage", "disabled", "unknown")
WEAK_WIFI_DBM = -75          # a camera heard weaker than this drops frames soon
AI_STALE_SECS = 120          # detection runs at least every 10 s on every camera


def _tunnel_health(t: dict) -> dict:
    """The tunnel's part: a warning only when it is KNOWN to be wrong. Not set
    up (a LAN-only home) or not enrolled yet are states, not faults; a config
    this app may not read is unknown."""
    warnings: list[str] = []
    if not t.get("installed") or not t.get("edge_id"):
        state = DISABLED
    elif not t.get("readable"):
        state = UNKNOWN
    elif not t.get("keyed"):
        state = OUTAGE
        warnings.append(t.get("reason") or "the tunnel does not route this edge_id")
    elif t.get("connected") is False:
        state = OUTAGE
        warnings.append("no connection to the tunnel server — live view and remote access "
                        "are unavailable (check the internet; restart frpc)")
    else:
        state = OPERATIONAL
    return {"state": state, "routes_edge_id": t.get("keyed"), "connected": t.get("connected"),
            "detail": t.get("reason"), "warnings": warnings}


def _rounded(value, digits: int = 1):
    return round(value, digits) if isinstance(value, (int, float)) else None


def _secs(value) -> int | None:
    """Whole seconds, or None for "not running / unknown"."""
    return int(round(value)) if isinstance(value, (int, float)) else None


def _judged(warnings: list[str], worst: str = DEGRADED) -> str:
    return worst if warnings else OPERATIONAL


def _network_part(internet_warnings: list[str]) -> dict:
    """The device's two networks: the internet (by Ethernet cable) and the camera
    hotspot it runs on its own Wi-Fi radio."""
    from api.setup_routes import internet_latency_ms
    from common import hotspot
    from common.net import default_gateway, link_speed_mbps, wired_uplink
    port = wired_uplink()
    spot = hotspot.health()
    spot_warn: list[str] = []
    if spot.get("supported") and not spot.get("on"):
        spot_warn.append("the camera hotspot is off — cameras cannot connect to this device")
    if spot.get("power_save") == "on":
        spot_warn.append("hotspot power saving is on — it delays camera video; switch it off "
                         "(bash setup/install_hotspot.sh)")
    spot["state"] = (UNKNOWN if not spot.get("supported") else
                     OUTAGE if not spot.get("on") else _judged(spot_warn))
    spot["warnings"] = spot_warn
    connected = not internet_warnings
    return {
        "state": OUTAGE if internet_warnings else OPERATIONAL,
        "internet": {"connected": connected,
                     "latency_ms": internet_latency_ms() if connected else None},
        "uplink": {"interface": port.iface, "cable": port.cable, "ip": port.ip,
                   "gateway": default_gateway(), "speed_mbps": link_speed_mbps(port.iface)},
        "hotspot": spot,
        "warnings": internet_warnings,
    }


def _ai_part(st, cameras_running: bool, rule_warnings: list[str]) -> dict:
    """Is the device really watching: the detector running and analysing frames,
    and the chain behind it (tracking, pose, falls, no-motion) switched on."""
    runner = getattr(st, "detection_runner", None)
    gallery = getattr(st, "gallery", None)
    metrics = getattr(st, "metrics_registry", None)
    last = getattr(runner, "last_inference_time", None)
    age = (clock.now() - last).total_seconds() if last else None
    warnings = list(rule_warnings)
    stopped = runner is None or not runner.is_running
    stale = not stopped and cameras_running and (age is None or age > AI_STALE_SECS)
    if stopped:
        warnings.insert(0, "AI detection is not running — no person, fall or no-motion "
                           "detection (restart the ceravis service)")
    elif stale:
        warnings.insert(0, f"AI detection has analysed no frame for "
                           f"{(age or AI_STALE_SECS) / 60:.0f} min although cameras deliver video")
    size = getattr(gallery, "size", None)
    return {
        "state": OUTAGE if stopped or stale else _judged(warnings),
        "detector_running": not stopped,
        "last_analysis_age_secs": _rounded(age),
        "gallery_size": size,
        "fall_detection": bool(getattr(st, "rule_engine", None) is not None and size),
        "pipelines": metrics.snapshot_all() if metrics is not None else [],
        "warnings": warnings,
    }


def _setup_part(st, cameras: list[dict]) -> dict:
    """Is this device set up to watch over someone? A care recipient assigned
    (and their pictures enrolled — the AI part warns when not), and cameras
    added. Says so in a line, so a fleet shows at once which devices run but
    serve nobody yet. Never names the person."""
    gallery = getattr(st, "gallery", None)
    if account_recipient() is None:
        recipient = "not_assigned"
    elif not getattr(gallery, "size", 0):
        recipient = "not_enrolled"
    else:
        recipient = "assigned"
    warnings = []
    if recipient == "not_assigned":
        warnings.append("no care recipient assigned yet — sign in on this device and assign one")
    if not cameras:
        warnings.append("no cameras added yet — add the home's cameras in this device's setup")
    return {"state": _judged(warnings), "care_recipient": recipient,
            "cameras_added": len(cameras), "warnings": warnings}


def _camera_hosts() -> dict[str, str]:
    """camera_id -> the camera's IP/host, from its RTSP URL."""
    from urllib.parse import urlsplit
    out = {}
    for cam in CameraConfig().get_all():
        try:
            out[cam.camera_id] = urlsplit(cam.rtsp_url).hostname or ""
        except ValueError:
            out[cam.camera_id] = ""
    return out


@router.get("/api/v1/system/health")
def system_health(request: Request):
    """Every part of this device in detail, each with its own `state` (one
    vocabulary: operational / degraded / outage / disabled / unknown) and
    `warnings` (empty = fine). The /system/status rules decide the shared
    warnings, so the two reports can never disagree; the parts only this report
    has (network, hotspot, AI, Wi-Fi signal) add their own.

    The fleet agent records it in full every minute (logs/health.jsonl) and
    sends a short summary as the heartbeat (edge/fleet, docs/fleet-heartbeat-
    api.md, edge/LOGS.md); it adds what only it can measure — the hardware
    and the systemd services — so a device whose app is down still reports.
    Loopback read, like /system/status."""
    g = _gather(request)
    rules = _warnings(g)

    def part(name: str) -> list[str]:
        return [m for p, _, m in rules if p == name]

    # ---- the networks: internet and the camera hotspot ----
    network = _network_part(part("internet"))
    by_ip = {d.get("ip"): d for d in network["hotspot"].get("devices") or [] if d.get("ip")}

    # ---- cameras: one entry each, with its own warnings ----
    live = g["live"]
    rec = {c["camera_id"]: c for c in g["recording"]["cameras"]}
    hosts = _camera_hosts()
    cameras = []
    for c in g["cameras"]:
        cid = c["camera_id"]
        st = live.get(cid)
        stream = getattr(getattr(st, "health_state", None), "value", None) or "offline"
        warnings = [m for p, cam, m in rules if p == "camera" and cam == cid]
        last = getattr(st, "last_frame_time", None)
        last_age = (clock.now() - last).total_seconds() if last else None
        station = by_ip.get(hosts.get(cid)) or {}
        signal = station.get("signal_avg_dbm", station.get("signal_dbm"))
        if c["enabled"] and stream != "running":
            warnings.insert(0, f"{cid}: {stream} — no video from this camera right now")
        if c["enabled"] and signal is not None and signal < WEAK_WIFI_DBM:
            warnings.append(f"{cid}: weak Wi-Fi signal ({signal} dBm) — move the camera "
                            f"closer to the device or clear what is in between")
        state = (DISABLED if not c["enabled"] else OUTAGE if stream != "running"
                 else _judged(warnings))
        r = rec.get(cid, {})
        cameras.append({
            "camera_id": cid, "name": c["name"], "room": c["room"], "enabled": c["enabled"],
            "state": state, "stream": stream,
            "fps": _rounded(getattr(st, "current_fps", None)),
            "resolution": c["resolution"], "codec": c["codec"], "profile": c["profile"],
            "uptime_secs": _secs(getattr(st, "connected_secs", None)),
            "reconnects": getattr(st, "reconnect_count", 0) if st else 0,
            "last_frame_age_secs": _rounded(last_age),
            "frames_captured": getattr(st, "frames_captured", 0) if st else 0,
            "stream_ready": c["path_ready"], "viewers": c["readers"],
            "recording": {"on": bool(r.get("recording")), "writing_ok": r.get("writing_ok", True),
                          "newest_segment_age_secs": r.get("newest_segment_age_secs"),
                          "segments_last_hour": r.get("segments_last_hour", 0)},
            "night_vision": bool((c.get("illumination") or {}).get("night_vision")),
            "ptz": c["ptz"],
            "wifi": {"ip": hosts.get(cid) or None, "signal_dbm": signal,
                     **{k: station.get(k) for k in ("tx_retries", "tx_failed", "tx_bitrate_mbps",
                                                    "connected_secs")}},
            "warnings": warnings,
        })

    # ---- services this app runs or talks to ----
    mtx = getattr(g["state"], "mediamtx", None)
    mtx_up = g["backbone_up"]
    mtx_warn = part("mediamtx")
    if not mtx_up:
        mtx_warn += [f"mediamtx log: {line}" for line in _backbone_log()[-3:]]
    cloud = g["cloud"]
    queue = cloud.get("outbox") or {}
    last_call = cloud.get("last_call") or {}
    app_warn = part("app_server")
    unreachable = cloud.get("configured") and network["state"] == OUTAGE
    if unreachable:
        app_warn.insert(0, "the CERAVIS app server cannot be reached — alerts wait on this "
                           "device and go out as soon as it is back")
    app_state = (DISABLED if not cloud.get("configured") else
                 OUTAGE if unreachable else _judged(app_warn))
    time_warn = part("time")
    services = {
        "mediamtx": {
            "state": OPERATIONAL if mtx_up else OUTAGE,
            "uptime_secs": _secs(getattr(mtx, "uptime_secs", None)),
            "restarts": getattr(mtx, "restarts", 0) if mtx else 0,
            "warnings": mtx_warn,
        },
        "app_server": {
            "state": app_state,
            "url": settings.ceravis_api_base_url or None,
            "recent_calls": cloud.get("recent_calls", 0),
            "recent_errors": cloud.get("recent_errors", 0),
            "last_call_ok": last_call.get("ok") if last_call else None,
            "uploads_pending": queue.get("pending", 0),
            "oldest_pending_secs": queue.get("oldest_pending_age_secs"),
            "warnings": app_warn,
        },
        "tunnel": _tunnel_health(_tunnel_status()),
        "time_sync": {"state": _judged(time_warn), **g["time"], "warnings": time_warn},
    }

    # ---- this app, and its AI ----
    running = any(c["stream"] == "running" for c in cameras)
    ai = _ai_part(g["state"], running, part("ai"))
    setup = _setup_part(g["state"], g["cameras"])
    storage = g["storage"]
    listed = {c["camera_id"] for c in cameras}
    # A camera warning for a camera not listed (removed meanwhile) is never lost.
    backend_warn = part("backend") + [m for p, cam, m in rules
                                      if p == "camera" and cam not in listed]
    backend = {
        "state": _judged(backend_warn),
        "version": settings.app_version,
        "commit": g["code"].get("commit"),
        "restart_needed": g["code"].get("restart_needed"),
        "uptime_secs": round(time.monotonic() - _STARTED),
        "device_id": settings.device_id,
        "recording": {"enabled": g["recording"]["enabled"],
                      "cameras_recording": len(g["recording"]["recording_now"])},
        "storage": {k: storage.get(k) for k in ("recordings_gb", "retention_hours",
                                                "oldest_segment", "event_snapshots_gb",
                                                "event_retention_days")},
        "warnings": backend_warn,
    }

    # ---- the board's own sensors (tegrastats), when this app reads them ----
    monitor = getattr(g["state"], "system_monitor", None)
    snap = monitor.snapshot() if monitor is not None else {}
    hw_warn = part("hardware")
    hardware = {
        "state": _judged(hw_warn),
        "last_boot": reboot.last_boot(),
        "gpu_load_pct": snap.get("gpu_pct"),
        "cpu_load_pct": snap.get("cpu_pct_avg"),
        "temperature_c": {k: snap[f] for k, f in (("cpu", "cpu_temp_c"), ("gpu", "gpu_temp_c"),
                                                  ("tj", "junction_temp_c")) if f in snap},
        "board_power_w": snap.get("power_in_w"),
        "over_current_events": snap.get("overcurrent_events"),
        "warnings": hw_warn,
    }
    return {"backend": backend, "hardware": hardware, "network": network, "setup": setup,
            "ai": ai, "services": services, "cameras": cameras}


# ---- reboot ---------------------------------------------------------
# Read is open (an operator must be able to see whether tonight's reboot is
# scheduled); the ACT is password-gated. See maintenance/reboot.py for why the
# edge_id match that guards every other control endpoint is not enough here.

@router.get("/api/v1/system/reboot")
def reboot_status(request: Request):
    """Will this device reboot tonight, and can it be rebooted now."""
    return reboot.status(getattr(request.app.state, "outbox", None))


@router.post("/api/v1/system/reboot")
async def reboot_now(request: Request):
    """Manual reboot. Body: {"password": "...", "force": false}.

    `force` overrides only the SAFETY deferral (queued alerts), never the
    password — a human can decide an undelivered alert is acceptable; nobody
    gets to skip authenticating.
    """
    body = await request.json() if await request.body() else {}
    ok, err = reboot.verify_password(str(body.get("password") or ""))
    if not ok:
        # Deliberately no distinction between "wrong password" and "no password
        # set" in the STATUS code — both are 401, so probing tells an attacker
        # nothing. The message is still specific, because it is only returned
        # over the LAN/tunnel to someone already reaching this endpoint.
        call_log.record("event", False, label="WARNING · Reboot refused",
                        error=err or "unauthorised")
        raise HTTPException(401, err or "unauthorised")

    block = reboot.safety_block(getattr(request.app.state, "outbox", None))
    if block and not bool(body.get("force")):
        raise HTTPException(409, f"{block}. Retry with force=true to override.")

    reboot.perform("manual request", "operator (API)")
    return {"rebooting": True, "in_secs": settings.reboot_delay_secs,
            "forced_past": block}
