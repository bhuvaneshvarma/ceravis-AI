from __future__ import annotations

"""
WiFi-camera onboarding: find ONVIF cameras on the network (household LAN or
the Jetson's own hotspot) and interrogate one for its streams.

    GET  /api/v1/discovery/scan     -> cameras answering WS-Discovery
    POST /api/v1/discovery/probe    -> {xaddr, username, password} ->
         device info + main stream URI (raw quality, untouched) + a dedicated
         recording URI (second profile standardized to ~1080p when supported)
         + PTZ capability. The wizard feeds this straight into the camera form.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
def scan():
    """WS-Discovery multicast probe — a few seconds' scan of the local subnet."""
    return {"cameras": discover(timeout=settings.onvif_discovery_secs)}


@router.post("/probe")
def probe_camera(req: ProbeRequest):
    """Full interrogation of one discovered camera (proves the credentials)."""
    try:
        result = probe(req.xaddr, req.username, req.password,
                       record_height=settings.record_target_height)
    except OnvifError as exc:
        raise HTTPException(400, str(exc))
    return result
