from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from configuration.account_config import AccountConfig
from integration.ceravis_api import CeravisApiError, get_user_details, is_configured


router = APIRouter(prefix="/api/v1/account", tags=["Account"])
account_config = AccountConfig()
logger = logging.getLogger("account")


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
