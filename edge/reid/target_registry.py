from __future__ import annotations

import time
from threading import RLock

from config.settings import settings


class TargetRegistry:
    """
    Per-camera "who is the target right now" lock.

    This is the lightweight, torch-free alternative to a full BoT-SORT
    appearance tracker: ByteTrack provides stable frame-to-frame IDs, and
    ReID (FAISS) decides which of those IDs is the enrolled recipient.
    Once locked:
      - pose/posture focus on the target crop only (others are skipped),
      - ReID stops re-embedding the other tracks while the lock is fresh,
      - if the target's track is lost and re-appears under a NEW id,
        ReID re-matches it against the gallery and re-locks — i.e. the
        target persists across occlusions ("remember the assigned ID").
    """

    def __init__(self) -> None:
        self._lock = RLock()
        # camera_id -> (track_id, recipient_id, last_seen_monotonic)
        self._targets: dict[str, tuple[int, str, float]] = {}

    def lock(self, camera_id: str, track_id: int, recipient_id: str) -> None:
        with self._lock:
            self._targets[camera_id] = (track_id, recipient_id, time.monotonic())

    def get(self, camera_id: str) -> int | None:
        """Current target track_id for a camera, or None if none/expired."""
        with self._lock:
            entry = self._targets.get(camera_id)
            if entry is None:
                return None
            track_id, _rid, seen = entry
            if time.monotonic() - seen > settings.target_lock_ttl_secs:
                self._targets.pop(camera_id, None)
                return None
            return track_id

    def recipient(self, camera_id: str) -> str | None:
        with self._lock:
            entry = self._targets.get(camera_id)
            return entry[1] if entry else None

    def is_fresh(self, camera_id: str) -> bool:
        return self.get(camera_id) is not None

    def all(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for cam in list(self._targets.keys()):
            tid = self.get(cam)
            if tid is not None:
                out[cam] = tid
        return out
