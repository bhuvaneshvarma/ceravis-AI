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
    # WHY this track is the target (set on the target only):
    #   "verified"   the gallery matched it — every daytime lock;
    #   "continuity" night: held on the tracker's continuity because the camera
    #                is on infrared and nothing contradicts it;
    #   "context"    night: the only person in the home, not contradicting the
    #                recipient's looks (see settings.night_context_lock).
    identity_basis: str | None = None
    # The picture the identity was judged on: "color" or "ir" (night vision).
    modality: str | None = None
