from __future__ import annotations

"""
Target lock manager — the policy layer on top of the tracker.

The tracker (BoT-SORT) gives appearance-stable IDs. This decides WHICH of those
IDs is the enrolled recipient and keeps that decision through trouble:

  acquire   : no target yet -> hybrid-match EVERY track's feature against the
              gallery, lock the clear winner.
  verify    : target visible & not occluded -> re-confirm with the gallery;
              refresh the lock; allow adaptive capture.
  freeze    : another person overlaps the target (occlusion) -> HOLD the lock,
              do NOT update identity, do NOT capture adaptive (so the intruder
              can't poison the gallery) until they separate.
  release   : target visible, not occluded, but mismatches N times -> drop the
              lock at once (don't ride the TTL on the wrong person).
  reacquire : target track vanished (full occlusion) -> when a track reappears
              near the last known spot AND matches appearance, re-lock and the
              recipient keeps following — original identity restored.

It is deliberately tracker-agnostic and side-effect-free: `update()` returns a
plan; the caller applies it to the registry / identity buffer / adaptive queue.
"""

import time
from dataclasses import dataclass, field

from config.settings import settings


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def _center(b) -> tuple[float, float]:
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0


@dataclass(slots=True)
class _CamState:
    recipient_id: str | None = None
    track_id: int | None = None
    last_cx: float = 0.0
    last_cy: float = 0.0
    last_w: float = 1.0
    last_score: float = 0.0
    mismatch_streak: int = 0
    lost_since: float = 0.0              # monotonic when the track went missing; 0 = present


@dataclass(slots=True)
class LockOutcome:
    target_track_id: int | None = None
    recipient_id: str | None = None
    # track_id -> (recipient_id|None, is_target, score, view_label)
    identities: dict = field(default_factory=dict)
    adaptive: tuple | None = None        # (track_id, recipient_id, score) or None
    recency: float | None = None         # recency score behind an acquire/reacquire
    released: bool = False               # lock was dropped this tick (confirmed mismatch)
    # The target is locked but NOT confirmed on this camera this tick (its track
    # is gone and no confident reacquire here). The caller widens the search —
    # drops the per-camera registry focus so EVERY camera is scanned to re-find
    # them next door — without forgetting who the recipient is.
    lost: bool = False


class TargetLockManager:
    def __init__(self, gallery, recency=None, memory=None) -> None:
        self._gallery = gallery
        # Short-term appearance memory of the confirmed target. Used ONLY on the
        # acquire / reacquire paths — steady-state verification stays on the
        # gallery, so a drifting recent memory can never quietly redefine who the
        # recipient is. See reid/recency_buffer.py.
        self._recency = recency
        # Auto-harvested pool of confirmed NON-recipients (reid/track_memory.py).
        # A re-find candidate that looks more like a known bystander than like the
        # recipient is vetoed — this is what stops the "same-score other person"
        # from inheriting the lock. Optional: None simply disables the veto.
        self._memory = memory
        self._state: dict[str, _CamState] = {}

    def forget(self, camera_id: str) -> None:
        self._state.pop(camera_id, None)

    def update(self, camera_id: str, boxes: dict[int, tuple],
               feat_for) -> LockOutcome:
        """
        boxes:    track_id -> (x1, y1, x2, y2)
        feat_for: track_id -> smooth feature (np.ndarray) or None
        """
        st = self._state.setdefault(camera_id, _CamState())
        out = LockOutcome()
        if not boxes:
            return out

        occluded = self._occlusion(boxes)

        # ---- have a locked target whose track is still alive --------------
        if st.recipient_id is not None and st.track_id in boxes:
            tid = st.track_id
            box = boxes[tid]
            st.lost_since = 0.0
            # FREEZE when another person is OCCLUDING or merely NEAR the target:
            # a padded crop then contains their pixels, so verifying / learning /
            # pushing recency off it would file the neighbour under the recipient.
            # Hold the lock on BoT-SORT's own appearance-fused association and
            # resume the instant they separate — no foreign pixels, ever.
            near = (settings.target_proximity_freeze
                    and not self._alone(boxes, tid))
            if near or occluded.get(tid, 0.0) >= settings.target_occlusion_iou:
                self._remember(st, box)
                out.target_track_id = tid
                out.recipient_id = st.recipient_id
                out.identities[tid] = (st.recipient_id, True, st.last_score, None)
                return out

            feat = feat_for(tid)
            if feat is None:                       # no fresh look this tick; hold
                out.target_track_id = tid
                out.recipient_id = st.recipient_id
                out.identities[tid] = (st.recipient_id, True, st.last_score, None)
                return out

            m = self._gallery.match(feat)
            if m.is_match and m.recipient_id == st.recipient_id:
                st.mismatch_streak = 0
                st.last_score = m.score
                self._remember(st, box)
                out.target_track_id = tid
                out.recipient_id = st.recipient_id
                out.identities[tid] = (st.recipient_id, True, m.score, m.view_label)
                if self._alone(boxes, tid):
                    out.adaptive = (tid, st.recipient_id, m.score)
                return out

            # mismatch while clearly visible -> count toward release
            st.mismatch_streak += 1
            if st.mismatch_streak >= settings.target_mismatch_release_checks:
                st.recipient_id = None
                st.track_id = None
                st.mismatch_streak = 0
                out.released = True
                return out
            out.target_track_id = tid             # tentatively hold one more tick
            out.recipient_id = st.recipient_id
            out.identities[tid] = (st.recipient_id, True, m.score, None)
            return out

        # ---- locked recipient but its track vanished -> reacquire ----------
        if st.recipient_id is not None:
            cand = self._best_match(boxes, feat_for, want=st.recipient_id,
                                    spatial_from=st, acquire=False,
                                    camera_id=camera_id)
            if cand is not None:
                tid, score, view, box = cand
                out.recency = self._last_recency
                st.track_id = tid
                st.mismatch_streak = 0
                st.last_score = score
                st.lost_since = 0.0
                self._remember(st, box)
                out.target_track_id = tid
                out.recipient_id = st.recipient_id
                out.identities[tid] = (st.recipient_id, True, score, view)
                if (occluded.get(tid, 0.0) < settings.target_occlusion_iou
                        and self._alone(boxes, tid)):
                    out.adaptive = (tid, st.recipient_id, score)
                return out
            # Not on this camera this tick. Widen the search (drop the registry
            # focus so every camera is scanned) but KEEP who the recipient is for
            # the fast same-camera reacquire — until it has been too long, when we
            # forget the per-camera memory so a later look-alike near the old spot
            # cannot inherit the lock and a clean gallery acquire takes over.
            now = time.monotonic()
            if st.lost_since == 0.0:
                st.lost_since = now
            out.lost = True
            if now - st.lost_since > settings.target_reacquire_ttl_secs:
                self._state.pop(camera_id, None)
            return out

        # ---- no target yet -> acquire the clearest match ------------------
        cand = self._best_match(boxes, feat_for, want=None, spatial_from=None,
                                acquire=True, camera_id=camera_id)
        if cand is not None:
            tid, score, view, box = cand
            rid = self._last_match_rid
            out.recency = self._last_recency
            st.recipient_id = rid
            st.track_id = tid
            st.mismatch_streak = 0
            st.last_score = score
            self._remember(st, box)
            out.target_track_id = tid
            out.recipient_id = rid
            out.identities[tid] = (rid, True, score, view)
            if (occluded.get(tid, 0.0) < settings.target_occlusion_iou
                    and self._alone(boxes, tid)):
                out.adaptive = (tid, rid, score)
        return out

    # ---- helpers -----------------------------------------------------
    _last_match_rid: str | None = None
    _last_recency: float | None = None

    def _best_match(self, boxes, feat_for, want, spatial_from, acquire=False,
                    camera_id=None):
        """Return (track_id, score, view, box) of the best match, or None — the
        PREMIUM re-find, precision over recall by design.

        `want` restricts to one recipient; `spatial_from` adds a distance gate;
        `acquire` demands a stronger score for taking a brand-new lock.

        The score is the gallery score FUSED with short-term recency whenever a
        live memory exists (see _fuse) — so a candidate that clears the general
        gallery bar but looks nothing like the target's last few seconds is
        vetoed. On top of that, a candidate that looks more like a KNOWN
        bystander than like the recipient is vetoed by the negative pool. And the
        winner must beat the runner-up TRACK by a clear margin: two people who
        both look like the recipient lock NOBODY — we keep searching rather than
        gamble on which one is real. Steady-state verification above stays on the
        gallery alone; all of this applies only to acquire / reacquire."""
        self._last_match_rid = None
        self._last_recency = None
        scored = []          # (fused_score, tid, view, box, recipient_id, recency)
        for tid, box in boxes.items():
            feat = feat_for(tid)
            if feat is None:
                continue
            m = self._gallery.match(feat)
            if not m.is_match:
                continue
            if want is not None and m.recipient_id != want:
                continue
            if spatial_from is not None and not self._within(spatial_from, box):
                continue
            score, rec = self._fuse(m.recipient_id, m.score, feat)
            if score is None:
                continue                      # recency veto — see _fuse
            if self._memory is not None:
                neg = self._memory.negative_score(feat)
                if (neg >= settings.reid_negative_veto_score
                        and neg - score >= settings.reid_negative_veto_margin):
                    continue                  # looks more like a known non-target
                # Cross-camera boost: does this candidate also match the
                # recipient's OWN recent exit from another room? If so, lift it
                # above a gallery look-alike. Boost only — never lowers the bar.
                if camera_id is not None:
                    cont = self._memory.target_continuation(
                        camera_id, feat, m.recipient_id)
                    if (cont is not None and cont >= settings.track_memory_min_score
                            and cont > score):
                        w = settings.reid_continuation_weight
                        score = (1.0 - w) * score + w * cont
            scored.append((score, tid, m.view_label, box, m.recipient_id, rec))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        best = scored[0]
        if (len(scored) > 1
                and best[0] - scored[1][0] < settings.reid_target_pick_margin):
            return None                       # a look-alike ties the winner — pick nobody
        if acquire and best[0] < settings.reid_acquire_min_score:
            return None                       # not confident enough for a NEW lock
        self._last_match_rid = best[4]
        self._last_recency = best[5]
        return (best[1], best[0], best[2], best[3])

    def _fuse(self, rid, gallery_score: float, feat):
        """(fused_score, recency_score), or (None, recency) when recency VETOES.

        With NO live memory — cold start, or the target has been gone longer than
        reid_recency_ttl_secs — the gallery score stands unchanged. That fallback
        is deliberate and load-bearing: recency must never be able to block the
        very first lock, which is exactly when no memory can exist yet."""
        if self._recency is None or not rid:
            return gallery_score, None
        rec = self._recency.score(rid, feat)
        if rec is None:
            return gallery_score, None        # no memory — gallery alone
        if rec < settings.reid_recency_min_score:
            return None, rec                  # nothing like the last sighting
        w = settings.reid_recency_weight
        return (1.0 - w) * gallery_score + w * rec, rec

    @staticmethod
    def _alone(boxes: dict[int, tuple], tid: int) -> bool:
        """Is this track far enough from EVERY other person to learn from safely?

        Adaptive capture used to be gated on the occlusion threshold — actual
        box overlap (IoU >= target_occlusion_iou). That is far too late. Long
        before two boxes overlap, a neighbour is already inside the target's
        crop: crop_person pads by crop_padding_frac, so someone merely STANDING
        NEAR contributes their pixels to the vector we are about to file under
        the recipient's name.

        The failure that causes is self-reinforcing and hard to see. A
        contaminated vector enters the gallery, which raises the neighbour's
        match score, which makes them a better candidate next time, which
        captures more of them. The gallery drifts onto the wrong person one
        confident sample at a time, and every score involved looks healthy
        throughout.

        So learning requires SOLITUDE, not merely non-overlap: no other track's
        centre within reid_adaptive_solitude_frac box-widths."""
        me = boxes.get(tid)
        if me is None:
            return False
        mx, my = _center(me)
        span = max(1.0, me[2] - me[0])
        limit = settings.reid_adaptive_solitude_frac * span
        for other, box in boxes.items():
            if other == tid:
                continue
            ox, oy = _center(box)
            if ((mx - ox) ** 2 + (my - oy) ** 2) ** 0.5 <= limit:
                return False
        return True

    @staticmethod
    def _occlusion(boxes: dict[int, tuple]) -> dict[int, float]:
        ids = list(boxes)
        occ = {t: 0.0 for t in ids}
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                v = _iou(boxes[ids[i]], boxes[ids[j]])
                occ[ids[i]] = max(occ[ids[i]], v)
                occ[ids[j]] = max(occ[ids[j]], v)
        return occ

    @staticmethod
    def _remember(st: _CamState, box) -> None:
        cx, cy = _center(box)
        st.last_cx, st.last_cy = cx, cy
        st.last_w = max(box[2] - box[0], 1.0)

    @staticmethod
    def _within(st: _CamState, box) -> bool:
        cx, cy = _center(box)
        dist = ((cx - st.last_cx) ** 2 + (cy - st.last_cy) ** 2) ** 0.5
        return dist <= settings.target_reacquire_max_dist_frac * st.last_w
