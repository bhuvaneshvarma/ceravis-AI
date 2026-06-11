from __future__ import annotations

from fastapi import APIRouter

from configuration.recipient_config import RecipientConfig
from enrollment.enrollment_manager import EnrollmentManager
from schemas.recipient import Recipient


router = APIRouter(prefix="/api/v1/recipients", tags=["Recipients"])

recipient_config = RecipientConfig()
enrollment_manager = EnrollmentManager()


@router.get("")
def list_recipients():
    return recipient_config.get_all()


@router.post("")
def create_recipient(recipient: Recipient):
    recipient_config.add(recipient)
    enrollment_manager.create_recipient_folder(recipient.recipient_id)
    return {"status": "created", "recipient_id": recipient.recipient_id}
