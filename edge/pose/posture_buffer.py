from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from pose.posture_classifier import Posture


@dataclass(slots=True)
class PostureRecord:
    camera_id: str
    track_id: int
    posture: Posture
    confidence: float
    timestamp: datetime
    torso_angle_deg: float
    knee_angle_deg: float


class PostureBuffer:
    """
    Per-track latest posture. camera_id -> { track_id -> PostureRecord }.

    Read by rules engine (FallRule, posture-based events) and the UI.
    """

    __slots__ = ("_lock", "_records")

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, dict[int, PostureRecord]] = {}

    def update(self, record: PostureRecord) -> None:
        with self._lock:
            self._records.setdefault(record.camera_id, {})[record.track_id] = record

    def get(self, camera_id: str, track_id: int) -> PostureRecord | None:
        with self._lock:
            return self._records.get(camera_id, {}).get(track_id)

    def get_all(self) -> dict[str, dict[int, PostureRecord]]:
        with self._lock:
            return {cid: dict(per) for cid, per in self._records.items()}
