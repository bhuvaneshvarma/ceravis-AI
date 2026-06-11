from pydantic import BaseModel


class Event(BaseModel):
    event_id: str
    event_type: str

    camera_id: str

    room_name: str

    zone_name: str | None = None

    recipient_id: str | None = None

    timestamp: str

    snapshot_path: str | None = None

    video_path: str | None = None