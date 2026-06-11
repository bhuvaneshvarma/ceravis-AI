from pydantic import BaseModel


class Alert(BaseModel):
    alert_id: str

    event_id: str

    severity: str

    title: str

    message: str