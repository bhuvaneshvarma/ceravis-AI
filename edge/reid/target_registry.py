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
        # The last recipient ever locked — remembered across a lapse so we can
        # tell "a target is known but currently unlocated" (searching) from
        # "no target has ever been identified".
        self._last_recipient: str | None = None

    def lock(self, camera_id: str, track_id: int, recipient_id: str) -> None:
        with self._lock:
            self._targets[camera_id] = (track_id, recipient_id, time.monotonic())
            self._last_recipient = recipient_id

    def unlock(self, camera_id: str) -> None:
        """Drop the lock immediately (e.g. confirmed mismatch) instead of
        waiting for the TTL to lapse on the wrong person."""
        with self._lock:
            self._targets.pop(camera_id, None)

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

    def searching(self) -> bool:
        """A recipient IS known but is not confirmed-fresh on any camera right
        now — the pipeline is hunting for the target across cameras (e.g. a
        room-to-room transition). While this holds, a freshly-appeared
        unidentified person could be the recipient arriving, so the visitor rule
        gives ReID a brief moment to claim them before treating them as a
        visitor. False before any target is ever identified and while one is
        locked, so it never suppresses ordinary visitors."""
        with self._lock:
            known = self._last_recipient is not None
        return known and not self.all()
