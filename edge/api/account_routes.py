from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from config.settings import settings
from configuration.account_config import AccountConfig
from configuration.camera_config import CameraConfig
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError,
    get_user_details,
    is_configured,
    room_to_enum,
    save_cameras,
)
from livestream.mediamtx_client import is_up as media_backbone_up
from livestream.mediamtx_client import stream_base, webrtc_url


router = APIRouter(prefix="/api/v1/account", tags=["Account"])
account_config = AccountConfig()
logger = logging.getLogger("account")


def _stream_base(request: Request) -> str:
    """Externally-reachable base for the live links handed to the cloud —
    delegates to livestream.mediamtx_client.stream_base (the ONE base builder,
    shared with tests/test_cloud.py): DEVICE_STREAM_BASE override, else this
    host with the scheme MediaMTX is actually serving."""
    return stream_base(request.headers.get("host") or "localhost")


class VerifyRequest(BaseModel):
    email: str
    phone: str | None = None


@router.post("/verify")
def verify(req: VerifyRequest):
    """
    Gate the setup wizard: look the email up on the CERAVIS app server. If the
    account exists, persist it (with the entered phone) and return it so the next
    screen can pre-fill. The phone is stored alongside — UserDetailsResponse has
    no phone field, so the email is what the server verifies.
    """
    email = (req.email or "").strip()
    if not email:
        return {"verified": False, "reason": "Email is required"}
    try:
        user = get_user_details(email)
    except CeravisApiError as exc:
        logger.warning("account verify failed for %s: %s", email, exc)
        return {"verified": False, "reason": str(exc)}
    if not user:
        return {"verified": False,
                "reason": "No CERAVIS account found for this email"}

    account = {
        "ceravisUserId": user.get("ceravisUserId"),
        "firstName": user.get("firstName"),
        "lastName": user.get("lastName"),
        "email": user.get("email") or email,
        "phone": (req.phone or "").strip(),
        "gender": user.get("gender"),
        "role": user.get("role"),
        "tier": user.get("tier"),
    }
    account_config.save(account)
    logger.info("account verified: user #%s (%s)",
                account["ceravisUserId"], account["email"])
    return {"verified": True, "user": account}


@router.post("/sync-cameras")
def sync_cameras(request: Request):
    """
    Push every registered camera to the app server for the verified patient:
    PUT /v1/ai/saveCamera with patientUserId + [{ device, model, supplier,
    room, url }]. `url` = the camera's WebRTC HTTPS live link (MediaMTX) —
    sub-second latency, plays natively in any modern browser.
    """
    acct = account_config.get()
    pid = acct.get("ceravisUserId")
    if not pid:
        return {"synced": False, "reason": "Account not verified — verify first"}
    cams = CameraConfig().get_all()
    if not cams:
        return {"synced": False, "reason": "No cameras registered yet"}

    base = _stream_base(request)
    # Fleet links route on the /<edge_id> path prefix; without EDGE_ID the pushed
    # URLs have no prefix and frp can't route them to this house.
    if settings.device_stream_base.strip() and not settings.edge_id.strip():
        logger.warning("sync-cameras: DEVICE_STREAM_BASE set but EDGE_ID empty — "
                       "fleet live links need the /<edge_id> prefix to route; set "
                       "EDGE_ID in jetson.env and re-sync")
        call_log.record(
            "event", False,
            label="Live links pushed without EDGE_ID — fleet routing will fail",
            error="set EDGE_ID (the /<edge_id> routing token) in jetson.env, "
                  "restart ceravis, then re-sync cameras")
    cameras = [{
        "device": c.camera_id,
        "model": "",                         # not collected on the edge
        "supplier": "",                      # not collected on the edge
        "room": room_to_enum(c.room_name),   # -> server CameraName enum
        "url": webrtc_url(c.camera_id, base),
    } for c in cams]
    logger.info("sync-cameras: pushing %d camera(s) for user #%s: %s",
                len(cameras), pid, [(c["room"], c["url"]) for c in cameras])

    # The links point at MediaMTX — if it isn't running they are dead on
    # arrival. Sync anyway (the server needs the cameras registered), but say
    # so loudly here AND on the monitor's sync console.
    backbone = media_backbone_up()
    if not backbone:
        logger.warning("sync-cameras: media backbone DOWN — pushed live links "
                       "will be dead until MediaMTX runs on this device")
        call_log.record(
            "event", False,
            label="Camera live links pushed while the media backbone is DOWN",
            error="links dead until MediaMTX runs — bash setup/"
                  "install_mediamtx.sh + restart ceravis, then re-sync cameras")

    try:
        result = save_cameras(pid, cameras)
    except CeravisApiError as exc:
        logger.warning("saveCamera failed (user #%s): %s", pid, exc)
        return {"synced": False, "reason": str(exc), "count": len(cameras)}
    logger.info("saveCamera: %d camera(s) sent for user #%s", len(cameras), pid)
    return {"synced": True, "count": len(cameras), "media_backbone": backbone,
            "patientUserId": pid, "cameras": cameras, "server": result}


@router.get("")
def get_account():
    """Return the stored verified account (so the wizard can resume) plus
    whether the app-server integration is configured on this device."""
    acct = account_config.get()
    return {
        "verified": bool(acct.get("ceravisUserId")),
        "user": acct or None,
        "configured": is_configured(),
    }
