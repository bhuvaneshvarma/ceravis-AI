from __future__ import annotations

"""
WiFi-camera onboarding: find ONVIF cameras on the network (household LAN or
the Jetson's own hotspot) and interrogate one for its streams.

    GET  /api/v1/discovery/scan          -> cameras answering discovery
         ?deep=1  also sweeps the local subnet on the ONVIF ports (use when the
                  WiFi AP blocks multicast between clients — "AP isolation")
    POST /api/v1/discovery/probe         -> {xaddr, username, password} ->
         device info + main stream URI (raw quality, untouched) + a dedicated
         recording URI (second profile standardized to ~1080p when supported)
         + PTZ capability. The wizard feeds this straight into the camera form.

The scan response also reports the interfaces/subnets it searched and how each
camera was found ("multicast" vs "unicast"), so a silent network is diagnosable
instead of just "nothing found".
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common import net
from config.settings import settings
from onvif.client import probe
from onvif.discovery import discover
from onvif.soap import OnvifError


router = APIRouter(prefix="/api/v1/discovery", tags=["Discovery"])
logger = logging.getLogger("onvif")


class ProbeRequest(BaseModel):
    xaddr: str
    username: str = ""
    password: str = ""


@router.get("/scan")
def scan(deep: bool = False):
    """Find ONVIF cameras. Multicast WS-Discovery first; falls back to a bounded
    subnet sweep when multicast is silent (or always, on a deep scan)."""
    cameras = discover(
        timeout=settings.onvif_discovery_secs,
        deep=deep,
        fallback=settings.onvif_unicast_fallback,
    )
    return {
        "cameras": cameras,
        "interfaces": net.local_ipv4s(),
        "subnets": [str(n) for n in net.local_ipv4_networks()],
        "deep": deep,
    }


@router.post("/probe")
def probe_camera(req: ProbeRequest):
    """Full interrogation of one discovered camera (proves the credentials)."""
    try:
        result = probe(req.xaddr, req.username, req.password,
                       record_height=settings.record_target_height,
                       record_bitrate_kbps=settings.record_target_bitrate_kbps,
                       record_fps=settings.record_target_fps,
                       prefer_h264=settings.record_prefer_h264)
    except OnvifError as exc:
        raise HTTPException(400, str(exc))
    return result
