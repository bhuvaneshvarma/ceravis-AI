from __future__ import annotations

"""
WiFi-camera onboarding: find ONVIF cameras on the network (household LAN or
the Jetson's own hotspot) and interrogate one for its streams.

    GET  /api/v1/discovery/scan          -> cameras answering discovery
         ?deep=1  also sweeps the local subnet on the ONVIF ports (use when the
                  WiFi AP blocks multicast between clients — "AP isolation")
    POST /api/v1/discovery/probe         -> {xaddr, username, password} ->
         device info + every media profile + the main stream URI (raw quality)
         + PTZ capability. The wizard feeds this straight into the camera form.
         READ-ONLY: nothing on the camera is reconfigured. The main stream is
         the only one we take, and it is what live view, the AI and recording
         all consume.

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
        result = probe(req.xaddr, req.username, req.password)
    except OnvifError as exc:
        raise HTTPException(400, str(exc))
    return result
