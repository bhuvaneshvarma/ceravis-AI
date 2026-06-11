from pydantic import BaseModel


class Recipient(BaseModel):
    recipient_id: str
    full_name: str
    is_primary_recipient: bool = True
    notes: str | None = None