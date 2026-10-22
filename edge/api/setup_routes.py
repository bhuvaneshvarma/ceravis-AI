"""
First-run readiness — what the device's very first screen (ui/welcome.html)
shows when someone opens its address before it has been set up: is every part
installed and running, how to reach it, and whether it is ready to sign in.

    GET /api/v1/setup/state

Before the first successful sign-in (the device is not yet "commissioned") the
answer is public and complete, including the camera hotspot's name and password
when asked from the device's own network (never through the tunnel). Once a
care account has signed in, the root address goes to the normal pages and this
answer is reduced to {commissioned: true} for anyone not signed in.

Each check is cheap and best-effort: a dev PC answers too, with what it lacks.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request

from api.auth import signed_in
from api.control_auth import FLEET_EDGE_KEY
from common import hotspot
from common.device_identity import identity
from common.net import local_ipv4s
from config.account_config import patient_user_id
from config.settings import settings

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

_MIN_FREE_GB = 10.0
_internet: tuple[float, bool] = (0.0, False)


def commissioned() -> bool:
    """A care account has signed in on this device at least once."""
    return patient_user_id() is not None


def _reachable() -> bool:
    """Can the device reach the app server (TCP 443)? Cached for 30 s."""
    global _internet
    if time.monotonic() - _internet[0] < 30:
        return _internet[1]
    u = urlsplit(settings.ceravis_api_base_url)
    ok = False
    if u.hostname:
        try:
            with socket.create_connection((u.hostname, u.port or 443), timeout=2):
                ok = True
        except OSError:
            ok = False
    _internet = (time.monotonic(), ok)
    return ok


def _unit_active(unit: str) -> bool | None:
    try:
        out = subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                             text=True, timeout=3)
        return out.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return None


def _check(cid: str, label: str, ok: bool | None, level: str, detail: str = "") -> dict:
    return {"id": cid, "label": label, "ok": ok, "level": level, "detail": detail}


def _checks(request: Request) -> list[dict]:
    from integration.ceravis_api import api_key_problem, is_configured
    from integration.edge_provision import helper_installed
    from livestream.mediamtx_client import is_up
    from maintenance.reboot import clock_synchronized

    st = request.app.state
    engines = {"person detection": settings.detection_model_path,
               "pose": settings.pose_model_path}
    missing = [n for n, p in engines.items() if not Path(p).is_file()]
    extras = {"face identity": settings.face_recognizer_path,
              "re-identification": settings.reid_model_path}
    missing_extra = [n for n, p in extras.items() if not Path(p).is_file()]
    free_gb = None
    try:
        free_gb = shutil.disk_usage(settings.data_dir).free / 1e9
    except OSError:
        pass
    key_problem = api_key_problem() if is_configured() else "app server not configured"
    hs_ssid = hotspot.configured_ssid()
    hs_on = hotspot.is_active() if hs_ssid else False
    mon = getattr(st, "system_monitor", None)
    heat = mon.problems() if mon is not None and hasattr(mon, "problems") else []
    fleet = _unit_active("ceravis-fleet-agent")

    return [
        _check("service", "CERAVIS service running", True, "required"),
        _check("models", "AI models built", not missing, "required",
               "missing: " + ", ".join(missing) if missing else ""),
        _check("models_extra", "Face and re-identification models", not missing_extra,
               "recommended", "missing: " + ", ".join(missing_extra) if missing_extra else ""),
        _check("media", "Video server (MediaMTX) running", is_up(), "required"),
        _check("pipeline", "Camera pipeline running",
               getattr(st, "camera_manager", None) is not None, "required"),
        _check("disk", "Enough free storage", free_gb is not None and free_gb >= _MIN_FREE_GB,
               "required", f"{free_gb:.0f} GB free" if free_gb is not None else ""),
        _check("internet", "Internet connection", _reachable(), "required",
               urlsplit(settings.ceravis_api_base_url).hostname or ""),
        _check("app_key", "App server key installed", key_problem is None, "required",
               key_problem or ""),
        _check("clock", "Clock synchronised", clock_synchronized(), "recommended"),
        _check("tunnel", "Remote access tunnel installed", helper_installed(),
               "recommended"),
        _check("hotspot", "Camera hotspot on", hs_on, "recommended", hs_ssid),
        _check("fleet", "Fleet agent running", fleet, "recommended"),
        _check("temperature", "Temperature and power normal", not heat, "recommended",
               "; ".join(heat)),
    ]


@router.get("/state")
def setup_state(request: Request) -> dict:
    done = commissioned()
    if done and not signed_in(request):
        return {"commissioned": True}
    ident = identity()
    checks = _checks(request)
    ready = all(c["ok"] for c in checks if c["level"] == "required")
    port = 8000
    lan = [f"http://{ip}:{port}" for ip in local_ipv4s() if ip != hotspot.ADDRESS]
    out = {
        "commissioned": done,
        "ready": ready,
        "device": {"name": ident.name, "code": ident.code, "source": ident.source,
                   "version": settings.app_version},
        "checks": checks,
        "access": {"lan": lan, "local_name": f"http://{ident.name}.local:{port}",
                   "hotspot": f"http://{hotspot.ADDRESS}:{port}"},
        "hotspot": {"ssid": hotspot.configured_ssid(), "active": hotspot.is_active()},
    }
    # The password is for the person standing at the device: shown only before
    # setup is complete and never through the tunnel.
    if not done and FLEET_EDGE_KEY not in request.scope:
        saved = hotspot.saved_credentials()
        if saved.get("ssid") and saved.get("ssid") == out["hotspot"]["ssid"]:
            out["hotspot"]["password"] = saved.get("password", "")
    return out
