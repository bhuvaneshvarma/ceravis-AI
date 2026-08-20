from __future__ import annotations

"""
Short-term appearance memory of the CONFIRMED target — the "how do they look
RIGHT NOW" half of identity.

The enrolled gallery answers a general question: does this person look like the
recipient, across every outfit and pose we ever stored. That generality is what
makes it survive a wardrobe change, and it is also what makes it blunt at the
moment it matters most — reacquisition. Two people can both clear a general
0.55 threshold; only one of them was standing in that doorway three seconds ago.

This buffer holds the last N raw embeddings of the target while they were
confidently matched and NOT occluded, per recipient, with a TTL. On acquire and
reacquire it contributes two things:

  boost  — the true target scores higher, because they still look exactly like
           they did seconds ago (same clothes, same light, same camera).
  veto   — a candidate that clears the gallery but looks nothing like the last
           few seconds of the real target is REJECTED outright. This is the
           false-pick guard: a look-alike who squeaks over the general
           threshold cannot take the ID while a live recent memory exists.

Raw `curr` features are stored rather than the EMA, and scoring takes the max
cosine over the window. A handful of genuinely different recent looks is more
discriminative than one averaged look — the same set-to-set logic the gallery
uses, applied to a sliding window of seconds instead of a library of months.

Vectors only. No frames are retained.
"""

import time
from collections import deque
from threading import RLock

import numpy as np

from config.settings import settings


class RecencyBuffer:
    """recipient_id -> deque[(monotonic_ts, L2-normalized embedding)]."""

    __slots__ = ("_lock", "_mem")

    def __init__(self) -> None:
        self._lock = RLock()
        self._mem: dict[str, deque] = {}

    # ---- write -------------------------------------------------------
    def push(self, recipient_id: str, emb: np.ndarray) -> None:
        """Remember one confident, non-occluded look at the target."""
        if not settings.reid_recency_enabled or not recipient_id or emb is None:
            return
        v = np.asarray(emb, dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n == 0.0:
            return
        v = v / n
        with self._lock:
            dq = self._mem.get(recipient_id)
            if dq is None or dq.maxlen != settings.reid_recency_max:
                # (re)size on first use or after a settings change
                dq = deque(dq or (), maxlen=settings.reid_recency_max)
                self._mem[recipient_id] = dq
            dq.append((time.monotonic(), v))

    # ---- read --------------------------------------------------------
    def score(self, recipient_id: str, query: np.ndarray) -> float | None:
        """Max cosine between the query and the target's live recent looks.

        None means "no usable memory" — no entries, or every entry has aged out
        past reid_recency_ttl_secs. Callers MUST treat None as 'fall back to the
        gallery alone', never as a zero score: a cold start has no memory yet and
        must not be blocked from acquiring."""
        if not settings.reid_recency_enabled or not recipient_id:
            return None
        q = np.asarray(query, dtype=np.float32).ravel()
        n = float(np.linalg.norm(q))
        if n == 0.0:
            return None
        q = q / n
        cutoff = time.monotonic() - settings.reid_recency_ttl_secs
        with self._lock:
            dq = self._mem.get(recipient_id)
            if not dq:
                return None
            while dq and dq[0][0] < cutoff:      # drop aged entries on read
                dq.popleft()
            if not dq:
                return None
            live = [v for _ts, v in dq if v.shape[0] == q.shape[0]]
        if not live:
            return None
        return float(np.max(np.stack(live, axis=0) @ q))

    def has_memory(self, recipient_id: str) -> bool:
        """True when a live (un-expired) memory exists for this recipient."""
        return self._live_count(recipient_id) > 0

    def _live_count(self, recipient_id: str) -> int:
        cutoff = time.monotonic() - settings.reid_recency_ttl_secs
        with self._lock:
            dq = self._mem.get(recipient_id)
            if not dq:
                return 0
            return sum(1 for ts, _v in dq if ts >= cutoff)

    # ---- maintenance -------------------------------------------------
    def forget(self, recipient_id: str) -> None:
        with self._lock:
            self._mem.pop(recipient_id, None)

    def stats(self) -> dict[str, int]:
        """Live entry count per recipient — for /ai/state and the monitor."""
        with self._lock:
            rids = list(self._mem)
        return {rid: self._live_count(rid) for rid in rids}
