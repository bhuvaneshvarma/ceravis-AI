from __future__ import annotations

"""
Per-track appearance features, kept OUT of the Track pydantic model.

The tracker computes an OSNet embedding per track (the same model that feeds the
FAISS gallery) and stashes it here. The ReID identity step reads it to decide
which track is the enrolled recipient — so the SAME embedding space drives both
frame-to-frame association and gallery matching, and we never embed twice.

We keep both:
  * smooth — the track's EMA feature (noise-robust): used for gallery matching.
  * curr   — the latest raw feature: used for adaptive online-learning capture.
"""

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

import numpy as np


@dataclass(slots=True)
class TrackFeature:
    smooth: np.ndarray
    curr: np.ndarray
    frame_id: int
    timestamp: datetime


class TrackFeatureBuffer:
    """camera_id -> { track_id -> TrackFeature }."""

    __slots__ = ("_lock", "_feats")

    def __init__(self) -> None:
        self._lock = RLock()
        self._feats: dict[str, dict[int, TrackFeature]] = {}

    def update(self, camera_id: str, track_id: int, smooth: np.ndarray,
               curr: np.ndarray, frame_id: int, timestamp: datetime) -> None:
        with self._lock:
            self._feats.setdefault(camera_id, {})[track_id] = TrackFeature(
                smooth=smooth, curr=curr, frame_id=frame_id, timestamp=timestamp)

    def get(self, camera_id: str, track_id: int) -> TrackFeature | None:
        with self._lock:
            return self._feats.get(camera_id, {}).get(track_id)

    def prune(self, camera_id: str, alive_ids: set[int]) -> None:
        """Drop features for tracks that no longer exist (avoid unbounded growth)."""
        with self._lock:
            per = self._feats.get(camera_id)
            if per is None:
                return
            for tid in [t for t in per if t not in alive_ids]:
                per.pop(tid, None)
