from __future__ import annotations

"""
Long-dwell welfare checks on the care recipient (CR).

Each is a repeating **75-minute slot**: WINDOW minutes quiet, then one snapshot
per minute for the next COUNT minutes, then the slot resets and repeats.

  • NO MOTION — the CR's **whole skeleton is frozen**: not one pose keypoint has
    moved for the full window. The serious case (possible collapse /
    unconsciousness): a CRITICAL `no_motion` alert fires at the window mark, then
    a `no_motion_snapshot` every minute to the end of the slot.
  • NO TRANSITION — the CR is still moving/active but hasn't changed posture
    (e.g. sitting on the couch knitting for hours) for the window. Snapshot-only
    `no_transition_snapshot` burst; NO alert. Suppressed while NO MOTION is active,
    so a frozen person only raises the emergency, not the routine check.

Definitions (kept deliberately simple + cheap for the 1 Hz rule tick):
  - "motion" = ANY confident pose keypoint drifting more than `pose_move_frac`
    of the body scale (torso length) from its anchored resting position — i.e.
    real movement of the head/arms/legs/torso, read straight off the YOLO-pose
    skeleton we already run. This is what makes NO MOTION mean *the body is
    genuinely immobile*: a seated person whose hands are busy (stitching) keeps
    resetting it even though their torso centre never moves — the old box-centre
    check would have wrongly called that "frozen". Keypoints below _MIN_KP_CONF
    are ignored; sub-pixel jitter stays under the threshold.
  - "transition" = the posture LABEL (sitting/standing/walking) changing.
"""

import uuid
from datetime import datetime, timezone
from math import hypot

from config.settings import settings
from pose.posture_classifier import (
    Posture, LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP)
from schemas.event import Event
from rules.rule_context import RuleContext


# postures that count as a "held" state for the no-transition check
_HELD = (Posture.SITTING, Posture.STANDING)

_MIN_KP_CONF = 0.20      # keypoints below this confidence are treated as missing
_MIN_SHARED_KPS = 5      # need this many confident shared points to judge motion


class _Burst:
    """One snapshot-per-minute burst counter (up to COUNT)."""

    def __init__(self) -> None:
        self.n = 0
        self.last: datetime | None = None

    def reset(self) -> None:
        self.n = 0
        self.last = None

    def done(self) -> bool:
        return self.n >= settings.stillness_burst_count

    def due(self, now: datetime) -> bool:
        if self.done():
            return False
        if self.last is not None and \
                (now - self.last).total_seconds() < settings.stillness_burst_interval_secs:
            return False
        self.n += 1
        self.last = now
        return True


class StillnessRule:
    FRESH_SECS = 5.0     # ignore stale per-camera track results (idle cameras)

    def __init__(self) -> None:
        self._cam: str | None = None
        self._kp_anchor: list | None = None          # resting-pose keypoints
        self._motion_still_since: datetime | None = None         # frozen since
        self._posture: Posture | None = None
        self._posture_since: datetime | None = None              # held since
        self._nm = _Burst()                                      # no-motion burst
        self._nt = _Burst()                                      # no-transition burst

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        now = datetime.now(timezone.utc)
        tgt = self._find_target(ctx, now)
        if tgt is None:
            self._reset()
            return []
        cam, tid, h, rid, posture, kps = tgt

        self._track_pose_motion(cam, kps, h, now)
        self._track_posture(posture, now)

        window = settings.stillness_window_secs
        motion_still = self._elapsed(self._motion_still_since, now)
        posture_hold = self._elapsed(self._posture_since, now)

        # NO MOTION (frozen) takes priority — it's the emergency. Completing the
        # slot restarts BOTH timers so a still-frozen CR keeps raising no_motion,
        # never falling through to no_transition.
        if motion_still >= window:
            return self._burst(self._nm, now, cam, tid, rid,
                               self._reset_motion_and_posture, motion=True)
        # else NO TRANSITION (active/moving but posture unchanged)
        if posture_hold >= window and posture in _HELD:
            return self._burst(self._nt, now, cam, tid, rid, self._reset_posture, motion=False)
        return []

    # ---- burst emission ---------------------------------------------
    def _burst(self, burst: _Burst, now, cam, tid, rid, on_done, *, motion) -> list[Event]:
        events: list[Event] = []
        if burst.due(now):
            detail = f"{burst.n}/{settings.stillness_burst_count}"
            if motion:
                # first snapshot of the slot is the CRITICAL alert; rest are snaps
                etype = "no_motion" if burst.n == 1 else "no_motion_snapshot"
            else:
                etype = "no_transition_snapshot"
            events.append(Event(
                event_id=str(uuid.uuid4()), event_type=etype, camera_id=cam,
                room_name="", recipient_id=rid, timestamp=now.isoformat(),
                track_id=tid, detail=detail))
        if burst.done():
            on_done(now)          # restart the 60-min quiet period (repeat the slot)
            burst.reset()
        return events

    # ---- state tracking ---------------------------------------------
    def _track_pose_motion(self, cam, kps, h, now) -> None:
        """Skeleton stillness: the frozen-clock only advances while EVERY confident
        keypoint stays within `pose_move_frac` of the body scale from its anchored
        resting pose. Any keypoint moving past that (a limb, the head, the whole
        body) re-anchors and restarts the clock — so 'no motion' means the whole
        body is immobile, not just that the torso centre held still."""
        if self._cam != cam:                         # first sight / camera hop
            self._cam = cam
            self._kp_anchor = kps
            self._motion_still_since = now if kps is not None else None
            self._nm.reset()
            return
        if kps is None:
            return                                   # transient pose dropout — hold
        if self._kp_anchor is None:                  # anchor the first usable pose
            self._kp_anchor = kps
            self._motion_still_since = now
            self._nm.reset()
            return
        scale = self._body_scale(kps, h)
        shift = self._max_kp_shift(self._kp_anchor, kps, scale)
        if shift is None:
            return                                   # too few shared points — hold
        if shift > settings.pose_move_frac:          # a limb / the body moved
            self._kp_anchor = kps
            self._motion_still_since = now
            self._nm.reset()

    def _track_posture(self, posture, now) -> None:
        if posture != self._posture:
            self._posture = posture
            self._posture_since = now
            self._nt.reset()

    def _reset_motion_and_posture(self, now) -> None:
        self._motion_still_since = now
        self._posture_since = now

    def _reset_posture(self, now) -> None:
        self._posture_since = now

    # ---- pose-motion helpers ----------------------------------------
    @staticmethod
    def _max_kp_shift(anchor, current, scale: float) -> float | None:
        """Largest keypoint displacement as a fraction of the body scale, over the
        keypoints confident in BOTH the anchor and the current frame. None when
        too few (< _MIN_SHARED_KPS) are shared to judge reliably."""
        worst = 0.0
        shared = 0
        for a, c in zip(anchor, current):
            if a.confidence >= _MIN_KP_CONF and c.confidence >= _MIN_KP_CONF:
                shared += 1
                d = hypot(c.x - a.x, c.y - a.y) / scale
                if d > worst:
                    worst = d
        return worst if shared >= _MIN_SHARED_KPS else None

    def _body_scale(self, kps, h) -> float:
        """Torso length (mid-shoulder → mid-hip) in px, for scale-invariant motion;
        falls back to the bbox height when the torso keypoints aren't confident."""
        sh = self._mid(kps, LEFT_SHOULDER, RIGHT_SHOULDER)
        hp = self._mid(kps, LEFT_HIP, RIGHT_HIP)
        if sh and hp:
            d = hypot(sh[0] - hp[0], sh[1] - hp[1])
            if d > 1.0:
                return d
        return max(h, 1.0)

    @staticmethod
    def _mid(kps, i, j):
        a, b = kps[i], kps[j]
        if a.confidence >= _MIN_KP_CONF and b.confidence >= _MIN_KP_CONF:
            return ((a.x + b.x) / 2.0, (a.y + b.y) / 2.0)
        return None

    @staticmethod
    def _elapsed(since: datetime | None, now: datetime) -> float:
        return (now - since).total_seconds() if since else 0.0

    # ---- target + pose lookup ---------------------------------------
    def _find_target(self, ctx: RuleContext, now: datetime):
        """(camera_id, track_id, bbox_height, recipient_id, posture, keypoints) of
        the locked recipient, or None. `keypoints` is the target's 17-pt pose (or
        None if pose isn't available this frame).

        FRESH tracks only, best ReID confidence on a tie: a camera the recipient
        left keeps its last TrackResult + is_target identity forever, and acting
        on that frozen snapshot would accumulate the no-motion clock into a false
        CRITICAL alert an hour after a room change."""
        best = None
        best_conf = -1.0
        for camera_id, result in ctx.fresh_tracks(now, self.FRESH_SECS).items():
            for t in result.tracks:
                ident = ctx.identities.get(camera_id, t.track_id)
                if not (ident and ident.is_target):
                    continue
                if ident.confidence > best_conf:
                    best_conf = ident.confidence
                    best = (camera_id, t, ident)
        if best is None:
            return None
        camera_id, t, ident = best
        rec = ctx.postures.get(camera_id, t.track_id)
        posture = rec.posture if rec is not None else Posture.UNKNOWN
        kps = self._keypoints_for(ctx, camera_id, t.track_id)
        return (camera_id, t.track_id, max(t.bbox.height, 1.0),
                ident.recipient_id, posture, kps)

    @staticmethod
    def _keypoints_for(ctx: RuleContext, camera_id: str, track_id: int):
        """The target's 17 COCO keypoints on this camera, or None.

        Pose runs TARGET-ONLY once the recipient is locked (see PoseRunner), so
        the buffer holds a single pose — the target's — which is exactly when
        this rule is active. Poses aren't tagged with a track_id in this pipeline,
        so prefer a track_id match if one ever appears, else take the most
        confident pose."""
        pr = ctx.poses.get(camera_id)
        if pr is None or not pr.poses:
            return None
        tagged = [p for p in pr.poses if p.track_id == track_id]
        pose = tagged[0] if tagged else max(
            pr.poses, key=lambda p: sum(k.confidence for k in p.keypoints))
        return pose.keypoints if len(pose.keypoints) >= 17 else None

    def _reset(self) -> None:
        self._cam = None
        self._kp_anchor = None
        self._motion_still_since = None
        self._posture = None
        self._posture_since = None
        self._nm.reset()
        self._nt.reset()
