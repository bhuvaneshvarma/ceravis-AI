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

    # Phase B: a "significant motion" summary is first/middle/last frames. When
    # set, ALL of these are sent (as a 3-snapshot nest) with the alert; falls
    # back to [snapshot_path] when empty.
    snapshot_paths: list[str] | None = None

    video_path: str | None = None

    # Which tracked person this event is about (lets the enricher draw the
    # right box and resolve the area). Set by the rules.
    track_id: int | None = None

    # Optional rule-specific context (e.g. "kitchen → living room"), folded
    # into the message by the enricher. Transient (not persisted).
    detail: str | None = None

    # Filled by EventEnricher: operator-facing alert fields.
    severity: str | None = None         # critical | warning | info
    title: str | None = None
    message: str | None = None

    # Operator acknowledgement (audit).
    acknowledged: bool = False
    ack_by: str | None = None
    ack_at: str | None = None