from pydantic import BaseModel


class EmergencyContact(BaseModel):
    name: str
    relation: str | None = None
    phone: str


class Recipient(BaseModel):
    recipient_id: str
    full_name: str
    is_primary_recipient: bool = True

    # Optional profile details collected by the setup wizard
    date_of_birth: str | None = None          # ISO date string
    device_id: str | None = None              # the edge device serving this home
    address: str | None = None
    emergency_contacts: list[EmergencyContact] = []
    medical_notes: str | None = None
    notes: str | None = None
