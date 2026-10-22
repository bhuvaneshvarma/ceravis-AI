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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common import hotspot


router = APIRouter(prefix="/api/v1/network", tags=["Network"])


class HotspotRequest(BaseModel):
    ssid: str
    password: str


@router.get("/hotspot")
def hotspot_state():
    device = hotspot.wifi_device()
    if device is None:
        return {"supported": False, "reason": "no WiFi adapter found",
                "active": False, "ssid": "", "clients": []}
    active = hotspot.is_active()
    return {
        "supported": True,
        "device": device,
        "active": active,
        "ssid": hotspot.configured_ssid(),
        "clients": hotspot.clients(device) if active else [],
    }


@router.post("/hotspot")
def hotspot_start(req: HotspotRequest):
    try:
        return hotspot.start(req.ssid, req.password)
    except hotspot.HotspotError as exc:
        raise HTTPException(exc.status, str(exc))


@router.post("/hotspot/stop")
def hotspot_stop():
    try:
        hotspot.stop()
    except hotspot.HotspotError as exc:
        raise HTTPException(exc.status, str(exc))
    return {"active": False}
