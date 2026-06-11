from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Keypoint(BaseModel):
    x: float
    y: float
    confidence: float


class PoseEstimation(BaseModel):
    """17-keypoint pose for one tracked person."""
    track_id: int | None
    camera_id: str
    frame_id: int
    timestamp: datetime
    keypoints: list[Keypoint]  # COCO order, length 17


class PoseResult(BaseModel):
    camera_id: str
    frame_id: int
    timestamp: datetime
    poses: list[PoseEstimation]
