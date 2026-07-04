from __future__ import annotations

"""
Device hotspot — the Jetson as the household cameras' own WiFi network.

Driven through NetworkManager (nmcli, AP mode + ipv4 shared = NM runs DHCP/NAT
for the clients). WiFi cameras join this network; discovery then finds them on
it. Endpoints:

    GET  /api/v1/network/hotspot         -> capability + live state + clients
    POST /api/v1/network/hotspot         -> {ssid, password} create/update + up
    POST /api/v1/network/hotspot/stop    -> down (config kept for next start)

The service user needs NetworkManager rights — setup/install_hotspot.sh
installs the polkit rule that grants them.
"""

import logging
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.settings import settings


router = APIRouter(prefix="/api/v1/network", tags=["Network"])
logger = logging.getLogger("network")

_CON = settings.hotspot_connection_name


def _run(args: list[str], timeout: float = 15.0) -> tuple[int, str]:
    """Run one command; (returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def _wifi_device() -> str | None:
    """The AP-capable WiFi interface (override via HOTSPOT_INTERFACE)."""
    if settings.hotspot_interface.strip():
        return settings.hotspot_interface.strip()
    code, out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return None


def _hotspot_active() -> bool:
    code, out = _run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"])
    return code == 0 and _CON in out.splitlines()


def _configured_ssid() -> str:
    code, out = _run(["nmcli", "-g", "802-11-wireless.ssid",
                      "connection", "show", _CON])
    return out.strip() if code == 0 else ""


def _clients(device: str) -> list[dict]:
    """Devices currently on the hotspot: MACs from the AP (`iw station dump`),
    hostnames/IPs from NetworkManager's dnsmasq leases where available."""
    macs: list[str] = []
    code, out = _run(["iw", "dev", device, "station", "dump"], timeout=5)
    if code == 0:
        macs = re.findall(r"Station\s+([0-9a-f:]{17})", out, re.I)

    leases: dict[str, dict] = {}
    for lf in Path("/var/lib/NetworkManager").glob("dnsmasq-*.leases"):
        try:
            for line in lf.read_text().splitlines():
                f = line.split()
                if len(f) >= 4:
                    leases[f[1].lower()] = {"ip": f[2], "hostname": f[3]}
        except OSError:
            continue
    return [{"mac": m,
             "ip": leases.get(m.lower(), {}).get("ip", ""),
             "hostname": leases.get(m.lower(), {}).get("hostname", "")}
            for m in macs]


class HotspotRequest(BaseModel):
    ssid: str
    password: str


@router.get("/hotspot")
def hotspot_state():
    device = _wifi_device()
    if device is None:
        return {"supported": False, "reason": "no WiFi adapter found",
                "active": False, "ssid": "", "clients": []}
    active = _hotspot_active()
    return {
        "supported": True,
        "device": device,
        "active": active,
        "ssid": _configured_ssid(),
        "clients": _clients(device) if active else [],
    }


@router.post("/hotspot")
def hotspot_start(req: HotspotRequest):
    ssid = req.ssid.strip()
    if not ssid:
        raise HTTPException(400, "SSID is required")
    if len(req.password) < 8:
        raise HTTPException(400, "WPA2 requires a password of at least 8 characters")
    device = _wifi_device()
    if device is None:
        raise HTTPException(400, "no WiFi adapter found on this device")

    _run(["nmcli", "connection", "delete", _CON])          # rebuild from scratch
    steps = [
        ["nmcli", "connection", "add", "type", "wifi", "ifname", device,
         "con-name", _CON, "autoconnect", "yes", "ssid", ssid],
        ["nmcli", "connection", "modify", _CON,
         "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
         "ipv4.method", "shared", "ipv6.method", "disabled",
         "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", req.password],
        ["nmcli", "connection", "up", _CON],
    ]
    for args in steps:
        code, out = _run(args, timeout=30)
        if code != 0:
            logger.warning("hotspot step failed: %s -> %s", " ".join(args[:4]), out)
            hint = (" (service user lacks NetworkManager rights — run "
                    "setup/install_hotspot.sh)" if "not authorized" in out.lower()
                    else "")
            raise HTTPException(502, f"hotspot setup failed: {out.strip()[:200]}{hint}")
    logger.info("hotspot up: ssid=%s device=%s", ssid, device)
    return {"active": True, "ssid": ssid, "device": device}


@router.post("/hotspot/stop")
def hotspot_stop():
    code, out = _run(["nmcli", "connection", "down", _CON], timeout=20)
    if code != 0 and "not an active" not in out.lower():
        raise HTTPException(502, f"hotspot stop failed: {out.strip()[:200]}")
    logger.info("hotspot down")
    return {"active": False}
