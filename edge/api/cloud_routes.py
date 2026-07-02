from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from cloud.cloud_switch import switch
from integration.ceravis_api import is_configured


router = APIRouter(prefix="/api/v1/cloud", tags=["Cloud"])


class SetRequest(BaseModel):
    on: bool


@router.get("/state")
def state():
    """Whether cloud forwarding is on. `configured` = a server URL is set."""
    return {"on": switch.is_on(), "configured": is_configured()}


@router.post("/toggle")
def toggle():
    """Flip cloud forwarding and return the new state (for the monitor button)."""
    return {"on": switch.toggle()}


@router.post("/set")
def set_mode(req: SetRequest):
    return {"on": switch.set(req.on)}
