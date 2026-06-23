from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from pydantic import BaseModel

from config.settings import settings
from configuration.account_config import AccountConfig
from configuration.camera_config import CameraConfig
from integration.ceravis_api import (
    CeravisApiError,
    get_user_details,
    is_configured,
    room_to_enum,
    save_cameras,
)


router = APIRouter(prefix="/api/v1/account", tags=["Account"])
account_config = AccountConfig()
logger = logging.getLogger("account")


def _slug(s: str) -> str:
    """URL-safe label, e.g. 'Kitchen Camera' -> 'kitchen-camera'."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _stream_base(request: Request) -> str:
    """Externally-reachable HTTP(S) base for the MJPEG camera streams handed to
    the cloud. Prefer the configured override (point it at a TLS reverse proxy
    for a real https link); otherwise reuse the host the browser used to reach
    us. A ws/wss override is normalized to http/https for the MJPEG link."""
    base = settings.device_stream_base.strip()
    if base:
        return (base.rstrip("/")
                .replace("wss://", "https://").replace("ws://", "http://"))
    scheme = "https" if request.url.scheme == "https" else "http"
    host = request.headers.get("host") or "localhost:8000"
    return f"{scheme}://{host}"


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
    POST /v1/ai/saveCamera with patientUserId + [{ device, model, supplier,
    room, url }]. `room` = the room label; `url` = the camera's WebSocket stream.
    """
    acct = account_config.get()
    pid = acct.get("ceravisUserId")
    if not pid:
        return {"synced": False, "reason": "Account not verified — verify first"}
    cams = CameraConfig().get_all()
    if not cams:
        return {"synced": False, "reason": "No cameras registered yet"}

    base = _stream_base(request)
    cameras = [{
        "device": c.camera_id,
        "model": "",                         # not collected on the edge
        "supplier": "",                      # not collected on the edge
        "room": room_to_enum(c.room_name),   # -> server CameraName enum
        # Last path segment is the camera's LABEL (its display name), not the
        # raw id — the MJPEG endpoint resolves name/room/id all the same.
        "url": f"{base}/stream.mjpeg/{_slug(c.camera_name) or c.camera_id}",
    } for c in cams]
    logger.info("sync-cameras: pushing %d camera(s) for user #%s: %s",
                len(cameras), pid, [(c["room"], c["url"]) for c in cameras])

    try:
        result = save_cameras(pid, cameras)
    except CeravisApiError as exc:
        logger.warning("saveCamera failed (user #%s): %s", pid, exc)
        return {"synced": False, "reason": str(exc), "count": len(cameras)}
    logger.info("saveCamera: %d camera(s) sent for user #%s", len(cameras), pid)
    return {"synced": True, "count": len(cameras),
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
