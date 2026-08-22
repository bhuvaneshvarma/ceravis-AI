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

Definitions:
  - "motion" = the verdict of rules/target_motion.py — a fixed-ROI pixel
    signature (primary, sensitive) fused with a conservative pose-keypoint
    channel, behind M-of-N hysteresis. That module owns the whole signal and
    documents why the previous max-over-keypoints statistic made no_motion
    unreachable: it called 72.9% of ticks "motion" on a motionless person.

    This is what makes NO MOTION mean *the body is genuinely immobile* while
    still detecting the minute movements that keep a seated, active person out
    of it: a person knitting keeps resetting the clock with their hands, which
    the pixel channel sees at a 2.3x margin over sensor noise.
  - "transition" = the posture LABEL (sitting/standing/walking) changing.
"""

import uuid
from datetime import datetime

from common import clock
from config.settings import settings
from pose.posture_classifier import Posture
from rules.target_motion import TargetMotionDetector
from schemas.event import Event
from rules.rule_context import RuleContext


# postures that count as a "held" state for the no-transition check
_HELD = (Posture.SITTING, Posture.STANDING)



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
        # ONE owner of "is the recipient moving" — pixels + pose, fused.
        # See rules/target_motion.py for why the old max-over-keypoints
        # statistic made no_motion unreachable.
        self._motion = TargetMotionDetector()
        self._motion_still: float = 0.0              # secs frozen, from the detector
        self._posture: Posture | None = None
        self._posture_since: datetime | None = None              # held since
        self._nm = _Burst()                                      # no-motion burst
        self._nt = _Burst()                                      # no-transition burst
        self.last_verdict = None                     # observability (/ai/stillness)

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        now = clock.now()
        s = ctx.find_recipient(now, self.FRESH_SECS)
        if s is None:
            self._reset()
            return []
        cam, tid = s.camera_id, s.track.track_id
        rid, posture, kps = s.identity.recipient_id, s.posture, s.keypoints

        frame = None
        if ctx.frames is not None:
            fd = ctx.frames.get(cam)
            frame = fd.frame if fd is not None else None

        verdict = self._motion.update(cam, kps, s.track.bbox, frame, now)
        self.last_verdict = verdict
        if verdict.moving:
            self._nm.reset()
        self._motion_still = verdict.still_secs
        self._track_posture(posture, now)

        window = settings.stillness_window_secs
        motion_still = self._motion_still
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
    def _track_posture(self, posture, now) -> None:
        if posture != self._posture:
            self._posture = posture
            self._posture_since = now
            self._nt.reset()

    def _reset_motion_and_posture(self, now) -> None:
        self._motion.reset()          # restart the frozen clock at zero
        self._motion_still = 0.0
        self._posture_since = now

    def _reset_posture(self, now) -> None:
        self._posture_since = now

    @staticmethod
    def _elapsed(since: datetime | None, now: datetime) -> float:
        return (now - since).total_seconds() if since else 0.0

    def _reset(self) -> None:
        self._motion.reset()
        self._motion_still = 0.0
        self._posture = None
        self._posture_since = None
        self._nm.reset()
        self._nt.reset()
