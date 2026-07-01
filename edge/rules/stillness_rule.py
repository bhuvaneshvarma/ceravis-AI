from __future__ import annotations

"""
Long-dwell welfare checks on the care recipient (CR).

Each is a repeating **75-minute slot**: WINDOW minutes quiet, then one snapshot
per minute for the next COUNT minutes, then the slot resets and repeats.

  • NO MOTION — the CR's body is frozen (tracked box CENTRE not drifting) for the
    full window. The serious case (possible collapse / unconsciousness): a
    CRITICAL `no_motion` alert fires at the window mark, then a `no_motion_snapshot`
    every minute to the end of the slot.
  • NO TRANSITION — the CR is still moving/active but hasn't changed posture
    (e.g. sitting on the couch, shifting around) for the window. Snapshot-only
    `no_transition_snapshot` burst; NO alert. Suppressed while NO MOTION is active,
    so a frozen person only raises the emergency, not the routine check.

Definitions (kept deliberately simple + cheap for the 1 Hz rule tick):
  - "motion" = the tracked box centre moving more than `no_motion_move_frac` of
    its own height (scale-invariant WHOLE-BODY movement) — NOT pose keypoints and
    NOT raw pixel / optical-flow motion. So breathing/tiny fidgets still read as
    frozen; standing / walking / shifting seat count as motion and reset it.
  - "transition" = the posture LABEL (sitting/standing/walking) changing.
"""

import uuid
from datetime import datetime, timezone
from math import hypot

from config.settings import settings
from pose.posture_classifier import Posture
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
    def __init__(self) -> None:
        self._cam: str | None = None
        self._anchor: tuple[float, float, float] | None = None   # cx, cy, h
        self._motion_still_since: datetime | None = None         # frozen since
        self._posture: Posture | None = None
        self._posture_since: datetime | None = None              # held since
        self._nm = _Burst()                                      # no-motion burst
        self._nt = _Burst()                                      # no-transition burst

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        now = datetime.now(timezone.utc)
        tgt = self._find_target(ctx)
        if tgt is None:
            self._reset()
            return []
        cam, tid, cx, cy, h, rid, posture = tgt

        self._track_motion(cam, cx, cy, h, now)
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
    def _track_motion(self, cam, cx, cy, h, now) -> None:
        if self._anchor is None or self._cam != cam:
            self._cam = cam
            self._anchor = (cx, cy, h)
            self._motion_still_since = now
            self._nm.reset()
            return
        ax, ay, _ = self._anchor
        if hypot(cx - ax, cy - ay) > settings.no_motion_move_frac * h:   # moved
            self._anchor = (cx, cy, h)
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

    @staticmethod
    def _elapsed(since: datetime | None, now: datetime) -> float:
        return (now - since).total_seconds() if since else 0.0

    def _find_target(self, ctx: RuleContext):
        """(camera_id, track_id, cx, cy, h, recipient_id, posture) of the locked
        recipient, or None."""
        for camera_id, result in ctx.tracks.get_all().items():
            for t in result.tracks:
                ident = ctx.identities.get(camera_id, t.track_id)
                if not (ident and ident.is_target):
                    continue
                b = t.bbox
                rec = ctx.postures.get(camera_id, t.track_id)
                posture = rec.posture if rec is not None else Posture.UNKNOWN
                return (camera_id, t.track_id, (b.x1 + b.x2) / 2.0,
                        (b.y1 + b.y2) / 2.0, max(b.height, 1.0),
                        ident.recipient_id, posture)
        return None

    def _reset(self) -> None:
        self._cam = None
        self._anchor = None
        self._motion_still_since = None
        self._posture = None
        self._posture_since = None
        self._nm.reset()
        self._nt.reset()
