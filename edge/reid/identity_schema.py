from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Identity(BaseModel):
    """Result of matching a track against the enrolled gallery."""
    track_id: int
    camera_id: str
    frame_id: int
    timestamp: datetime
    recipient_id: str | None
    is_target: bool         # True if matched to an enrolled recipient
    confidence: float       # cosine similarity, [-1, 1] (we clamp to [0, 1])
    view_label: str | None = None   # label of the best-matching enrolled view
                                     # (e.g. "left/sitting") — for snapshot notes
    # Cosine against the target's short-term recency window at the moment the
    # lock was (re)acquired. None = steady-state verify, or no live memory.
    recency_score: float | None = None
