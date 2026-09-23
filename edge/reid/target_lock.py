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

Night vision. On a camera streaming INFRARED (ingestion/illumination.py) the
appearance model is half-blind, so "does not match" mostly means "cannot tell",
not "someone else". The same machine then runs three night rules, each a
fallback that only acts on an infrared camera and only when the normal path
found nothing:

  hold      : verification inconclusive and nothing CONTRADICTS the lock
              (another enrolled person matching, or a known non-target's look)
              -> keep it on the tracker's continuity instead of counting a
              mismatch. A contradiction still releases it exactly as by day.
  rejoin    : the locked track vanished and ONE person reappears within the
              reacquire radius, uncontradicted -> the same person (someone
              re-detected in bed after a blanket hid them).
  context   : no lock, and the runner has established this is the only person
              in the home and the recipient is locked nowhere else -> once
              they have been seen steadily AND have moved (a white chair the
              infrared lifts into a "person" never moves), take them to be the
              recipient.

Face evidence (reid/face_identity.py) is folded into the same decisions, by
day only: a visible face that clearly is NOT the recipient vetoes a candidate
and releases a lock outright; one that clearly IS them lets a body match at the
verify bar take a new lock, keeps a lock through body drift (new clothes) and
lets that look be learned. No face in view = the body rules, unchanged. Bars
from a joint body+face test on the bench — see settings.face_*.

Every lock carries its BASIS — "verified" (a gallery match; every daytime lock),
"continuity" or "context" — so consumers and the monitor can say how sure it is.
Only a lock established by a strong gallery match and carried on the same
track ever teaches the gallery (`learnable`), which is how the infrared gallery
learns the recipient's real night look without ever learning a guess.

It is deliberately tracker-agnostic and side-effect-free: `update()` returns a
plan; the caller applies it to the registry / identity buffer / adaptive queue.
"""

import time
from dataclasses import dataclass, field

from config.settings import settings

VERIFIED, CONTINUITY, CONTEXT = "verified", "continuity", "context"


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
    was_frozen: bool = False            # last tick froze on a neighbour (huddle) — re-verify on exit
    basis: str = VERIFIED               # why this track is the target (see module doc)
    # The identity on THIS track was established by a strong gallery match
    # (>= reid_adaptive_min_score) and never broken since — the only lock the
    # infrared gallery may learn from.
    learnable: bool = False


@dataclass(frozen=True, slots=True)
class NightContext:
    """What the lock needs to know about the dark, per camera per tick."""
    ir: bool = False                    # this camera streams infrared right now
    # Set by the runner ONLY when a lone person here may be taken to be the
    # recipient: context identity enabled, one person in the whole home, the
    # recipient locked nowhere else. The recipient's id, else None.
    sole_recipient: str | None = None


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
    basis: str | None = None             # the target lock's basis this tick
    # The lock rests on a gallery verification (always, by day) — the caller
    # may harvest the OTHER tracks as known non-targets. A guess must never
    # teach the negative pool that the real recipient is someone else.
    trusted: bool = False
    # Infrared: `adaptive` is approved for the IR gallery (a learnable lock in
    # solitude). By day this is always False and `adaptive` keeps its meaning.
    learn_ir: bool = False
    # The target's own face confirmed them this tick: `adaptive` may be learned
    # even below reid_adaptive_min_score (their body looks different today).
    face_confirmed: bool = False


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
        # camera -> track_id -> [first seen (monotonic), first box, has moved].
        # Context identity needs a person seen steadily AND alive — a phantom
        # the infrared gain lifts off a chair can be steady, but never moves.
        self._seen: dict[str, dict[int, list]] = {}

    def forget(self, camera_id: str) -> None:
        self._state.pop(camera_id, None)

    def update(self, camera_id: str, boxes: dict[int, tuple],
               feat_for, night: NightContext | None = None,
               face_for=None) -> LockOutcome:
        """
        boxes:    track_id -> (x1, y1, x2, y2)
        feat_for: track_id -> smooth feature (np.ndarray) or None
        night:    None / ir=False = a colour camera: the daytime path, unchanged.
        face_for: (track_id, recipient_id) -> (face score, face width px) or
                  None — the track's latest face look against that recipient's
                  enrolled faces. None disables face evidence entirely.
        """
        self._face_for = face_for
        st = self._state.setdefault(camera_id, _CamState())
        out = LockOutcome()
        self._age(camera_id, boxes)
        if not boxes:
            return out
        ir = night is not None and night.ir

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
                st.was_frozen = True           # a huddle can swap ids under us
                self._remember(st, box)
                self._emit(out, st, tid, st.last_score)
                return out

            # Just came OUT of a freeze — the neighbour separated. BoT-SORT may
            # have handed our target's id to that other person during the huddle.
            # Before trusting the held id, re-pick the recipient among the now-
            # clean, separated tracks by their PRE-huddle appearance (recency,
            # frozen through the huddle) fused with the gallery, and move the lock
            # to the winner if the id was swapped. This is the one clean-frame
            # ReID check the swap needs — no new capture path, recency already
            # holds the last confident looks.
            if st.was_frozen:
                st.was_frozen = False
                cand = self._best_match(boxes, feat_for, want=st.recipient_id,
                                        spatial_from=st, acquire=False, ir=ir)
                if cand is None:
                    # Nobody re-verified after the huddle: the id may have been
                    # swapped under us, so nothing seen from here on may teach
                    # the gallery until a clean verification.
                    st.learnable = False
                elif cand[0] in boxes and cand[0] != tid:
                    tid = st.track_id = cand[0]
                    box = boxes[tid]
                    st.mismatch_streak = 0

            feat = feat_for(tid)
            if feat is None:                       # no fresh look this tick; hold
                self._emit(out, st, tid, st.last_score)
                return out

            m = self._match(feat, ir)
            face = self._face_verdict(tid, st.recipient_id, ir)
            if face == "veto":
                # A clear face that is NOT the recipient: the strongest possible
                # contradiction. Release now instead of riding out the mismatch
                # count on the wrong person.
                st.recipient_id = None
                st.track_id = None
                st.mismatch_streak = 0
                out.released = True
                return out
            if m.recipient_id == st.recipient_id and (m.is_match or face == "confirm"):
                st.mismatch_streak = 0
                st.last_score = m.score
                self._verified(st, m.score)
                self._remember(st, box)
                self._emit(out, st, tid, m.score, m.view_label)
                if self._alone(boxes, tid):
                    out.adaptive = (tid, st.recipient_id, m.score)
                    out.learn_ir = ir and st.learnable
                    out.face_confirmed = face == "confirm"
                return out

            if (ir and settings.night_hold_lock
                    and not self._contradicted(feat, m, st.recipient_id)):
                # NIGHT HOLD: the model cannot vouch for them in infrared, but
                # nothing says it is someone else — the tracker has followed
                # this person all along. A contradiction still releases below.
                st.mismatch_streak = 0
                if st.basis == VERIFIED:
                    st.basis = CONTINUITY
                self._remember(st, box)
                self._emit(out, st, tid, m.score)
                if st.learnable and self._alone(boxes, tid):
                    out.adaptive = (tid, st.recipient_id, m.score)
                    out.learn_ir = True
                return out

            # mismatch while clearly visible -> count toward release
            st.mismatch_streak += 1
            if st.mismatch_streak >= settings.target_mismatch_release_checks:
                st.recipient_id = None
                st.track_id = None
                st.mismatch_streak = 0
                out.released = True
                return out
            self._emit(out, st, tid, m.score)     # tentatively hold one more tick
            return out

        # ---- locked recipient but its track vanished -> reacquire ----------
        if st.recipient_id is not None:
            cand = self._best_match(boxes, feat_for, want=st.recipient_id,
                                    spatial_from=st, acquire=False,
                                    camera_id=camera_id, ir=ir)
            if cand is not None:
                tid, score, view, box = cand
                out.recency = self._last_recency
                st.track_id = tid
                st.mismatch_streak = 0
                st.last_score = score
                st.lost_since = 0.0
                self._verified(st, score)
                self._remember(st, box)
                self._emit(out, st, tid, score, view)
                if (occluded.get(tid, 0.0) < settings.target_occlusion_iou
                        and self._alone(boxes, tid)):
                    out.adaptive = (tid, st.recipient_id, score)
                    out.learn_ir = ir and st.learnable
                return out
            if ir and settings.night_hold_lock:
                rejoin = self._night_rejoin(st, boxes, feat_for)
                if rejoin is not None:
                    # NIGHT REJOIN: one person, right where the recipient was,
                    # nothing against them. A NEW track, so not learnable.
                    tid, score = rejoin
                    st.track_id = tid
                    st.mismatch_streak = 0
                    st.lost_since = 0.0
                    st.learnable = False
                    if st.basis == VERIFIED:
                        st.basis = CONTINUITY
                    self._remember(st, boxes[tid])
                    self._emit(out, st, tid, score)
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
                                acquire=True, camera_id=camera_id, ir=ir)
        if cand is not None:
            tid, score, view, box = cand
            rid = self._last_match_rid
            out.recency = self._last_recency
            st.recipient_id = rid
            st.track_id = tid
            st.mismatch_streak = 0
            st.last_score = score
            self._verified(st, score)
            self._remember(st, box)
            self._emit(out, st, tid, score, view)
            if (occluded.get(tid, 0.0) < settings.target_occlusion_iou
                    and self._alone(boxes, tid)):
                out.adaptive = (tid, rid, score)
                out.learn_ir = ir and st.learnable
            return out
        if (ir and settings.night_context_lock and night.sole_recipient
                and len(boxes) == 1):
            pick = self._context_pick(camera_id, boxes, feat_for,
                                      night.sole_recipient)
            if pick is not None:
                # NIGHT CONTEXT: the only person in the home, seen steadily,
                # not contradicting the recipient. Never teaches the gallery.
                tid, score = pick
                st.recipient_id = night.sole_recipient
                st.track_id = tid
                st.mismatch_streak = 0
                st.last_score = score
                st.basis = CONTEXT
                st.learnable = False
                self._remember(st, boxes[tid])
                self._emit(out, st, tid, score)
        return out

    # ---- helpers -----------------------------------------------------
    _last_match_rid: str | None = None
    _last_recency: float | None = None

    # ---- night-vision helpers ------------------------------------------
    @staticmethod
    def _emit(out: LockOutcome, st: _CamState, tid: int, score: float,
              view=None) -> None:
        """Publish `tid` as the target this tick, with the lock's basis."""
        out.target_track_id = tid
        out.recipient_id = st.recipient_id
        out.identities[tid] = (st.recipient_id, True, score, view)
        out.basis = st.basis
        out.trusted = (st.basis == VERIFIED
                       or (st.basis == CONTINUITY and st.learnable))

    @staticmethod
    def _verified(st: _CamState, score: float) -> None:
        """A gallery match just established the identity on this track."""
        st.basis = VERIFIED
        st.learnable = score >= settings.reid_adaptive_min_score

    def _match(self, feat, ir: bool):
        """Gallery match in the query's own modality. The daytime call is
        exactly the historical one."""
        if ir:
            return self._gallery.match(feat, modality="ir")
        return self._gallery.match(feat)

    _face_for = None

    def _face_verdict(self, tid: int, recipient_id: str | None, ir: bool) -> str | None:
        """'veto' / 'confirm' / None from the track's latest face look — by
        day only, and only for a face wide enough to be evidence."""
        if ir or self._face_for is None or not recipient_id or not settings.face_enabled:
            return None
        f = self._face_for(tid, recipient_id)
        if f is None:
            return None
        score, px = f
        if px < settings.face_min_px:
            return None
        if score < settings.face_veto_score:
            return "veto"
        if score >= settings.face_confirm_score:
            return "confirm"
        return None

    def _contradicted(self, feat, m, recipient_id: str) -> bool:
        """Positive evidence this is NOT the recipient: another enrolled person
        matches, or the look is closer to a known non-target than to the
        recipient. Blindness (no match at all) is not a contradiction."""
        if m is not None and m.is_match and m.recipient_id != recipient_id:
            return True
        if self._memory is not None and feat is not None:
            own = m.score if (m is not None and m.recipient_id == recipient_id) else 0.0
            neg = self._memory.negative_score(feat)
            if (neg >= settings.reid_negative_veto_score
                    and neg - own >= settings.reid_negative_veto_margin):
                return True
        return False

    def _night_rejoin(self, st: _CamState, boxes, feat_for):
        """(track_id, score) of the ONE person now standing where the lost
        recipient was, if nothing contradicts them — else None."""
        if len(boxes) != 1:
            return None
        tid, box = next(iter(boxes.items()))
        if not self._within(st, box):
            return None
        feat = feat_for(tid)
        if feat is None:
            return tid, st.last_score
        m = self._match(feat, True)
        if self._contradicted(feat, m, st.recipient_id):
            return None
        return tid, (m.score if m.recipient_id == st.recipient_id else 0.0)

    def _context_pick(self, camera_id: str, boxes, feat_for, recipient_id: str):
        """(track_id, score) when the lone person here may be taken to be the
        recipient: seen for night_context_min_track_secs, has MOVED, not
        contradicting them. No usable look (a person lying under a blanket fails
        the crop gate) is not a contradiction — it is simply no evidence."""
        tid = next(iter(boxes))
        rec = self._seen.get(camera_id, {}).get(tid)
        if (rec is None or not rec[2] or time.monotonic() - rec[0]
                < settings.night_context_min_track_secs):
            return None
        feat = feat_for(tid)
        if feat is None:
            return tid, 0.0
        m = self._match(feat, True)
        if self._contradicted(feat, m, recipient_id):
            return None
        return tid, (m.score if m.recipient_id == recipient_id else 0.0)

    def _age(self, camera_id: str, boxes) -> None:
        """Per-track bookkeeping: when first seen, and whether it has ever
        MOVED — its centre or height shifted by night_context_min_move_frac of
        its size since then. Box jitter on a static phantom stays far below."""
        seen = self._seen.setdefault(camera_id, {})
        now = time.monotonic()
        frac = settings.night_context_min_move_frac
        for tid, b in boxes.items():
            rec = seen.get(tid)
            if rec is None:
                seen[tid] = [now, b, False]
                continue
            if not rec[2]:
                b0 = rec[1]
                scale = max(b0[2] - b0[0], b0[3] - b0[1], 1.0)
                (cx0, cy0), (cx, cy) = _center(b0), _center(b)
                if max(abs(cx - cx0), abs(cy - cy0),
                       abs((b[3] - b[1]) - (b0[3] - b0[1]))) >= frac * scale:
                    rec[2] = True
        for tid in [t for t in seen if t not in boxes]:
            seen.pop(tid, None)

    def _best_match(self, boxes, feat_for, want, spatial_from, acquire=False,
                    camera_id=None, ir=False):
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
        gallery alone; all of this applies only to acquire / reacquire.

        `ir` matches in the infrared gallery against the infrared bars, with
        the infrared recency window."""
        self._last_match_rid = None
        self._last_recency = None
        scored = []  # (fused_score, tid, view, box, recipient_id, recency, face)
        for tid, box in boxes.items():
            feat = feat_for(tid)
            if feat is None:
                continue
            m = self._match(feat, ir)
            if not m.is_match:
                continue
            if want is not None and m.recipient_id != want:
                continue
            face = self._face_verdict(tid, m.recipient_id, ir)
            if face == "veto":
                continue                      # their face says someone else
            if spatial_from is not None and not self._within(spatial_from, box):
                continue
            score, rec = self._fuse(m.recipient_id, m.score, feat, ir)
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
            scored.append((score, tid, m.view_label, box, m.recipient_id, rec, face))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        best = scored[0]
        if (len(scored) > 1
                and best[0] - scored[1][0] < settings.reid_target_pick_margin):
            return None                       # a look-alike ties the winner — pick nobody
        bar = (settings.reid_ir_acquire_min_score if ir
               else settings.reid_acquire_min_score)
        if acquire and best[0] < bar and best[6] != "confirm":
            return None                       # not confident enough for a NEW lock
        self._last_match_rid = best[4]
        self._last_recency = best[5]
        return (best[1], best[0], best[2], best[3])

    def _fuse(self, rid, gallery_score: float, feat, ir: bool = False):
        """(fused_score, recency_score), or (None, recency) when recency VETOES.

        With NO live memory — cold start, or the target has been gone longer than
        reid_recency_ttl_secs — the gallery score stands unchanged. That fallback
        is deliberate and load-bearing: recency must never be able to block the
        very first lock, which is exactly when no memory can exist yet."""
        if self._recency is None or not rid:
            return gallery_score, None
        rec = (self._recency.score(rid, feat, "ir") if ir
               else self._recency.score(rid, feat))
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
