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

Each record also says which picture it came from — colour or infrared (see
ingestion/illumination.py) — because the two are matched against different
galleries. The tracker drops appearance history on a modality switch, so a
record is never a blend of the two.

A record also carries the track's recent USABLE face looks (reid/face_identity
.py) — a few, not one: a face decision is made on several looks, never on a
single glance. They are set separately and kept across the per-tick body
updates.
"""

import time
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
    modality: str = "color"
    # Recent usable face looks, oldest first: (unit face vector, width px,
    # monotonic time). At most _FACE_LOOKS are kept.
    face_looks: tuple = ()
    face_looked: float = 0.0          # monotonic time of the latest look, face or not

    @property
    def face(self) -> np.ndarray | None:
        """The latest usable face vector, or None."""
        return self.face_looks[-1][0] if self.face_looks else None

    @property
    def face_px(self) -> float:
        return self.face_looks[-1][1] if self.face_looks else 0.0

    @property
    def face_at(self) -> float:
        return self.face_looks[-1][2] if self.face_looks else 0.0


_FACE_LOOKS = 6


class TrackFeatureBuffer:
    """camera_id -> { track_id -> TrackFeature }."""

    __slots__ = ("_lock", "_feats")

    def __init__(self) -> None:
        self._lock = RLock()
        self._feats: dict[str, dict[int, TrackFeature]] = {}

    def update(self, camera_id: str, track_id: int, smooth: np.ndarray,
               curr: np.ndarray, frame_id: int, timestamp: datetime,
               modality: str = "color") -> None:
        with self._lock:
            per = self._feats.setdefault(camera_id, {})
            old = per.get(track_id)
            per[track_id] = TrackFeature(
                smooth=smooth, curr=curr, frame_id=frame_id, timestamp=timestamp,
                modality=modality,
                face_looks=old.face_looks if old else (),
                face_looked=old.face_looked if old else 0.0)

    def set_face(self, camera_id: str, track_id: int, face: np.ndarray | None,
                 face_px: float) -> None:
        """Record a face look on an existing track record — `face` None when
        the look found no usable face (the look itself still counts)."""
        with self._lock:
            rec = self._feats.get(camera_id, {}).get(track_id)
            if rec is not None:
                rec.face_looked = time.monotonic()
                if face is not None:
                    rec.face_looks = (rec.face_looks
                                      + ((face, float(face_px), rec.face_looked),))[-_FACE_LOOKS:]

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
