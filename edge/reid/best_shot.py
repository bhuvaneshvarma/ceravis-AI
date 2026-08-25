from __future__ import annotations

"""
The best few looks at each track, kept ready for the moment they are needed.

An identity question arrives at an awkward time: a new track appears, someone
re-enters a doorway, two people separate after crossing. Whatever crop happens
to be under the cursor at that instant is often the worst one available — a
person mid-stride is motion-blurred precisely when you most want to know who
they are.

The obvious response is to raise the frame rate during an identity event. That
is the wrong lever: a blurred subject at 20 Hz is still blurred. What actually
helps is CHOOSING better, not sampling more.

So every track keeps a small ring of its best recent crops, ranked by
reid/crop_quality. When an identity event fires we embed the BEST of the last
few seconds rather than the latest. The crops already exist — the tracker is
looking at them anyway — so this converts a load SPIKE into a load SELECTION and
costs no extra inference.

Two consumers, one buffer:
  * identity events   — embed the best crop instead of the current one;
  * visitor snapshots — send the sharpest frame of a moving visitor rather than
    the smear that motion usually produces.

Crops are kept as small JPEG-quality arrays for a few seconds only. Nothing is
written to disk here.
"""

import threading
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from common import clock
from config.settings import settings
from reid.crop_quality import Quality


@dataclass(slots=True)
class Shot:
    crop: np.ndarray
    quality: Quality
    timestamp: datetime
    frame_id: int


class BestShotBuffer:
    """camera_id -> track_id -> the best N shots, newest-quality-ranked.

    Bounded three ways, because an unbounded per-track store keyed on a
    monotonically increasing track_id is the exact leak shape that already bit
    PostureBuffer and IdentityBuffer: capacity per track, an age horizon, and
    prune() driven by the live track set.
    """

    __slots__ = ("_lock", "_shots")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._shots: dict[str, dict[int, list[Shot]]] = {}

    # ---- write -------------------------------------------------------
    def offer(self, camera_id: str, track_id: int, crop: np.ndarray,
              quality: Quality, frame_id: int) -> bool:
        """Offer a crop. Kept only if it beats the weakest shot held, so the
        buffer converges on this track's best looks rather than its latest."""
        if not quality.ok or crop is None or crop.size == 0:
            return False
        cap = max(1, settings.best_shot_capacity)
        shot = Shot(crop=crop.copy(), quality=quality,
                    timestamp=clock.now(), frame_id=frame_id)
        with self._lock:
            per = self._shots.setdefault(camera_id, {})
            shots = per.setdefault(track_id, [])
            if len(shots) < cap:
                shots.append(shot)
            else:
                worst = min(range(len(shots)), key=lambda i: shots[i].quality.score)
                if shots[worst].quality.score >= quality.score:
                    return False                  # nothing better on offer
                shots[worst] = shot
            shots.sort(key=lambda s: s.quality.score, reverse=True)
        return True

    # ---- read --------------------------------------------------------
    def best(self, camera_id: str, track_id: int) -> Shot | None:
        """The single best LIVE shot, or None when nothing usable is held.

        Age matters: a pin-sharp crop from thirty seconds ago is not evidence
        about who is standing there now, so stale shots are dropped on read
        rather than served."""
        shots = self.best_k(camera_id, track_id, 1)
        return shots[0] if shots else None

    def best_k(self, camera_id: str, track_id: int, k: int) -> list[Shot]:
        now = clock.now()
        horizon = settings.best_shot_max_age_secs
        with self._lock:
            shots = self._shots.get(camera_id, {}).get(track_id)
            if not shots:
                return []
            live = [s for s in shots
                    if (now - s.timestamp).total_seconds() <= horizon]
            if len(live) != len(shots):
                self._shots[camera_id][track_id] = live
            return live[:max(1, k)]

    def count(self, camera_id: str, track_id: int) -> int:
        with self._lock:
            return len(self._shots.get(camera_id, {}).get(track_id, ()))

    # ---- maintenance -------------------------------------------------
    def prune(self, camera_id: str, alive_ids: set[int]) -> None:
        """Drop tracks that no longer exist. Mirrors TrackFeatureBuffer.prune —
        the one buffer in the tree that never leaked."""
        with self._lock:
            per = self._shots.get(camera_id)
            if not per:
                return
            for tid in [t for t in per if t not in alive_ids]:
                per.pop(tid, None)

    def stats(self) -> dict:
        with self._lock:
            return {cam: {"tracks": len(per),
                          "shots": sum(len(v) for v in per.values())}
                    for cam, per in self._shots.items()}
