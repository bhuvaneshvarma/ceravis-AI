from __future__ import annotations

"""
The nightly REFRESH — the light form of nightly maintenance.

maintenance/reboot.scheduled_decision picks tonight's strength: a full reboot
once the device has been up REBOOT_INTERVAL_DAYS (weekly), and this refresh on
every other night, inside the same window and behind the same safety gates.

  1. The cameras are restarted ONE AT A TIME (ONVIF SystemReboot), each given
     time to stream again before the next — so two rooms are never dark
     together, and camera-side memory is fresh.
  2. The edge service is restarted cleanly: it is signalled (SIGUSR1), shuts
     down exactly as `systemctl stop` would, and exits with a code systemd
     restarts (main.py). Its heap, buffers, connections and GPU contexts all
     start fresh — the useful part of a reboot, in ~20 s instead of minutes.

It deliberately does NOT drop the kernel page cache: that memory is already
reclaimable on demand, and dropping it only makes the next reads slower.
"""

import logging
import os
import signal
import subprocess
import time

from config.settings import settings
from integration import call_log


logger = logging.getLogger("maintenance")

SERVICE_UNIT = "ceravis"


def restart_cameras() -> list[str]:
    """Reboot each ONVIF camera in turn; one report line per camera."""
    from configuration.camera_config import CameraConfig
    from livestream.mediamtx_client import source_state, stream_path
    from onvif.client import OnvifCamera

    report: list[str] = []
    for cam in CameraConfig().get_all():
        if not getattr(cam, "enabled", True) or not cam.onvif_xaddr:
            report.append(f"{cam.camera_id}: skipped (no ONVIF address)")
            continue
        try:
            OnvifCamera(cam.onvif_xaddr, cam.onvif_username or "",
                        cam.onvif_password or "").system_reboot()
        except Exception as exc:
            report.append(f"{cam.camera_id}: reboot refused ({exc})")
            continue
        time.sleep(15)                    # let it actually go down first
        deadline = time.monotonic() + settings.refresh_camera_ready_timeout_secs
        back = False
        while time.monotonic() < deadline:
            try:
                if source_state(stream_path(cam.camera_id))[0]:
                    back = True
                    break
            except Exception:
                pass                      # MediaMTX API blip — keep watching
            time.sleep(5)
        report.append(f"{cam.camera_id}: rebooted, "
                      + ("streaming again" if back else
                         f"NOT back within {settings.refresh_camera_ready_timeout_secs:.0f}s"))
    return report


def restart_service() -> bool:
    """Ask the edge service to restart itself cleanly (main.py, SIGUSR1)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", SERVICE_UNIT, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        pid = int(out or "0")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if pid <= 0:
        return False
    os.kill(pid, signal.SIGUSR1)
    return True


def perform(reason: str, actor: str) -> None:
    logger.warning("REFRESH (%s by %s): cameras one at a time, then the service",
                   reason, actor)
    call_log.record("event", True, label=f"INFO · Device refresh · {reason} ({actor})")
    lines = (restart_cameras() if settings.refresh_cameras
             else ["cameras: not restarted (REFRESH_CAMERAS=false)"])
    for line in lines:
        logger.info("refresh: %s", line)
    ok = restart_service()
    logger.info("refresh: edge service %s", "restarting" if ok
                else "NOT signalled (service not running)")
