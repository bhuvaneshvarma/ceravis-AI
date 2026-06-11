from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from detection.detection_schema import BoundingBox


class Track(BaseModel):
    """
    Single tracked person at one frame.

    track_id is stable within a camera over time.
    """
    track_id: int
    camera_id: str
    frame_id: int
    timestamp: datetime
    bbox: BoundingBox
    confidence: float


class TrackResult(BaseModel):
    """All tracks for a frame."""
    camera_id: str
    frame_id: int
    timestamp: datetime
    tracks: list[Track]
