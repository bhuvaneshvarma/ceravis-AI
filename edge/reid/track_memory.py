from __future__ import annotations

"""
Who was here, what they looked like, and which way they left.

Three jobs that all need the same evidence, so they share one store rather than
three parallel ones:

  CONTINUATION  When a track appears on camera B, do not search the whole
                gallery — search the handful of tracks that recently EXITED
                other cameras. Over a few seconds clothing is a near-perfect
                signal and the candidate set is tiny, which makes this both more
                accurate and far cheaper than an open-set search.

  RE-FIND       When the target leaves a room, their last good looks are exactly
                the evidence needed to recognise them entering the next one.
                Those looks are already in the best-shot ring; this is where
                they are kept once the track itself is gone.

  NEGATIVES     Every track confidently REJECTED as not-the-target contributes
                its embedding to an anonymous pool. That solves the bootstrap
                problem in the negative gallery: you cannot enrol household
                members before you know who they are, and no family will enrol
                every visitor. The pool self-populates with exactly the people
                who really walk through this house, with no names, no UI and no
                enrolment step.

Everything here is body appearance. Faces are deliberately NOT stored for
non-targets: the question this store answers is "is this the person who left the
kitchen four seconds ago", and over that horizon clothing is decisive, the
candidate set is small, and a face may not even be visible from behind. Faces
would cost more and add nothing.

Vectors and timestamps only — no frames are retained here.
"""

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from common import clock
from config.settings import settings


logger = logging.getLogger("reid.memory")


def _unit(v) -> np.ndarray | None:
    a = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(a))
    return None if n == 0.0 else a / n


@dataclass(slots=True)
class ExitRecord:
    """A track that was here and is not any more."""
    camera_id: str
    track_id: int
    left_at: datetime
    embeddings: list = field(default_factory=list)   # L2-normalised
    last_box: tuple = (0.0, 0.0, 0.0, 0.0)
    edge: str = ""                # which frame edge they left by
    recipient_id: str | None = None     # set when this was a CONFIRMED target
    quality: float = 0.0


class TrackMemory:
    """Live per-track appearance, exit records, and the negative pool.

    Bounded everywhere. An unbounded store keyed on a monotonically rising
    track_id is the exact leak shape that already bit PostureBuffer and
    IdentityBuffer, so every structure here has a cap and an age horizon.
    """

    __slots__ = ("_lock", "_live", "_exits", "_negatives")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # (camera_id, track_id) -> deque of unit embeddings
        self._live: dict[tuple[str, int], deque] = {}
        self._exits: deque = deque(maxlen=max(4, settings.track_memory_max_exits))
        self._negatives: deque = deque(
            maxlen=max(8, settings.reid_negative_pool_max))

    # ---- live tracks -------------------------------------------------
    def observe(self, camera_id: str, track_id: int, embedding) -> None:
        """Remember what this track currently looks like. Called for EVERY
        track, target or not — the whole point is that a stranger's appearance
        is what lets us tell them apart from the recipient later."""
        v = _unit(embedding)
        if v is None:
            return
        key = (camera_id, track_id)
        with self._lock:
            dq = self._live.get(key)
            if dq is None or dq.maxlen != settings.track_memory_per_track:
                dq = deque(dq or (), maxlen=max(1, settings.track_memory_per_track))
                self._live[key] = dq
            dq.append(v)

    def retire(self, camera_id: str, track_id: int, last_box=None,
               frame_w: int = 0, frame_h: int = 0,
               recipient_id: str | None = None, quality: float = 0.0) -> None:
        """The track is gone. Move what we know into an exit record.

        `recipient_id` is set only when this track was a CONFIRMED target, which
        is what makes the record usable for re-finding them rather than merely
        for continuation."""
        key = (camera_id, track_id)
        with self._lock:
            dq = self._live.pop(key, None)
            if not dq:
                return
            self._exits.append(ExitRecord(
                camera_id=camera_id, track_id=track_id, left_at=clock.now(),
                embeddings=list(dq), last_box=tuple(last_box or (0, 0, 0, 0)),
                edge=_exit_edge(last_box, frame_w, frame_h),
                recipient_id=recipient_id, quality=quality))

    def prune(self, camera_id: str, alive_ids: set[int], boxes=None,
              frame_w: int = 0, frame_h: int = 0) -> None:
        """Retire every track on this camera that no longer exists. Mirrors
        TrackFeatureBuffer.prune — the one buffer in the tree that never leaked."""
        with self._lock:
            gone = [k for k in self._live
                    if k[0] == camera_id and k[1] not in alive_ids]
        for cam, tid in gone:
            box = (boxes or {}).get(tid)
            self.retire(cam, tid, box, frame_w, frame_h)

    # ---- continuation search -----------------------------------------
    def candidates(self, camera_id: str, exclude_self: bool = True) -> list[ExitRecord]:
        """Recent exits that could plausibly be walking in here now.

        Restricted by TIME (a transit window) and by CAMERA (someone who left
        this same camera a moment ago is usually the same track re-appearing,
        which the tracker's own lost-buffer already handles better)."""
        now = clock.now()
        window = settings.track_memory_transit_secs
        with self._lock:
            out = []
            for rec in self._exits:
                if exclude_self and rec.camera_id == camera_id:
                    continue
                if (now - rec.left_at).total_seconds() > window:
                    continue
                out.append(rec)
        return out

    def target_continuation(self, camera_id: str, embedding,
                            recipient_id: str) -> float | None:
        """Best cosine between this candidate and the RECIPIENT's own recent exit
        looks from OTHER cameras, within the transit window — or None.

        This is the cross-camera half of re-find: when the recipient leaves one
        room, their last good looks are filed as a recipient-tagged exit record;
        when a track appears in the NEXT room, matching those looks is what tells
        the real recipient apart from a gallery look-alike who merely dresses
        similarly. Used only to BOOST an already-valid gallery match (never to
        manufacture one), so it sharpens precision without lowering the bar."""
        if not recipient_id:
            return None
        q = _unit(embedding)
        if q is None:
            return None
        best = -1.0
        for rec in self.candidates(camera_id):
            if rec.recipient_id != recipient_id or not rec.embeddings:
                continue
            for e in rec.embeddings:
                if e.shape[0] == q.shape[0]:
                    best = max(best, float(e @ q))
        return best if best >= 0.0 else None

    # ---- negative pool -----------------------------------------------
    def add_negative(self, embedding) -> None:
        """This track was confidently NOT the target. Remember the look.

        Anonymous by construction — no identity is attached, because none is
        known or needed. The pool exists to answer 'what kinds of people around
        here look confusingly like the recipient', which is what turns an
        open-set threshold into a discrimination problem."""
        v = _unit(embedding)
        if v is not None:
            with self._lock:
                self._negatives.append(v)

    def negative_score(self, embedding) -> float:
        """Best similarity to any known non-target. High means this query looks
        like somebody we have already ruled out — a reason to REJECT, never a
        reason to accept."""
        q = _unit(embedding)
        if q is None:
            return 0.0
        with self._lock:
            pool = [v for v in self._negatives if v.shape[0] == q.shape[0]]
        if not pool:
            return 0.0
        return float(np.max(np.stack(pool, axis=0) @ q))

    # ---- observability -----------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            return {
                "live_tracks": len(self._live),
                "exit_records": len(self._exits),
                "negatives": len(self._negatives),
                "recent_exits": [
                    {"camera": r.camera_id, "track": r.track_id,
                     "edge": r.edge, "was_target": bool(r.recipient_id),
                     "age_secs": round((clock.now() - r.left_at).total_seconds(), 1)}
                    for r in list(self._exits)[-5:]],
            }


def _exit_edge(box, frame_w: int, frame_h: int) -> str:
    """Which frame edge the track was nearest when it vanished.

    A weak signal on its own, but a useful prior: someone who left by the right
    edge of the hall camera is far more likely to appear on the camera that
    covers what is to the right of it."""
    if not box or not frame_w or not frame_h:
        return ""
    x1, y1, x2, y2 = box
    dists = {"left": x1, "right": frame_w - x2,
             "top": y1, "bottom": frame_h - y2}
    edge = min(dists, key=dists.get)
    span = frame_w if edge in ("left", "right") else frame_h
    return edge if dists[edge] <= span * settings.track_memory_edge_frac else "interior"
