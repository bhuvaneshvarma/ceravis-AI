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


@dataclass(slots=True)
class LockOutcome:
    target_track_id: int | None = None
    recipient_id: str | None = None
    # track_id -> (recipient_id|None, is_target, score, view_label)
    identities: dict = field(default_factory=dict)
    adaptive: tuple | None = None        # (track_id, recipient_id, score) or None
    released: bool = False               # lock was dropped this tick


class TargetLockManager:
    def __init__(self, gallery) -> None:
        self._gallery = gallery
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
            if occluded.get(tid, 0.0) >= settings.target_occlusion_iou:
                # FREEZE — keep the lock, change nothing, learn nothing.
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
                                    spatial_from=st)
            if cand is not None:
                tid, score, view, box = cand
                st.track_id = tid
                st.mismatch_streak = 0
                st.last_score = score
                self._remember(st, box)
                out.target_track_id = tid
                out.recipient_id = st.recipient_id
                out.identities[tid] = (st.recipient_id, True, score, view)
                if occluded.get(tid, 0.0) < settings.target_occlusion_iou:
                    out.adaptive = (tid, st.recipient_id, score)
            return out                            # still searching otherwise

        # ---- no target yet -> acquire the clearest match ------------------
        cand = self._best_match(boxes, feat_for, want=None, spatial_from=None)
        if cand is not None:
            tid, score, view, box = cand
            rid = self._last_match_rid
            st.recipient_id = rid
            st.track_id = tid
            st.mismatch_streak = 0
            st.last_score = score
            self._remember(st, box)
            out.target_track_id = tid
            out.recipient_id = rid
            out.identities[tid] = (rid, True, score, view)
            if occluded.get(tid, 0.0) < settings.target_occlusion_iou:
                out.adaptive = (tid, rid, score)
        return out

    # ---- helpers -----------------------------------------------------
    _last_match_rid: str | None = None

    def _best_match(self, boxes, feat_for, want, spatial_from):
        """Return (track_id, score, view, box) of the best gallery match, or None.
        `want` restricts to one recipient; `spatial_from` adds a distance gate."""
        best = None
        best_score = -1.0
        self._last_match_rid = None
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
            if m.score > best_score:
                best_score = m.score
                best = (tid, m.score, m.view_label, box)
                self._last_match_rid = m.recipient_id
        return best

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
