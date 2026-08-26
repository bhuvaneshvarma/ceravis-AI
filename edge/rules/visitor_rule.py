from __future__ import annotations

"""
Snapshots of people who are NOT the recipient, while they are MOVING.

Visitor v1 (removed in 2afcc77) fired on a fixed time cadence, so a visitor
asleep on the sofa produced exactly the same snapshot burst as one walking
around. This is the rebuild, and the difference is the whole point: motion is
the trigger, not the clock.

What counts as a visitor here is deliberately generous — ANY fresh track that is
not the locked recipient, INCLUDING one with no identity at all. v1 required
`ident is not None and not ident.is_target`, so a person the gallery had never
matched was invisible to it. That is backwards: an unidentified person is
precisely who a visitor is.

Five gates, each answering a different way this can go wrong:

  FRESH       a track from a frozen buffer is not somebody standing there;
              idle cameras keep their last TrackResult forever.
  NOT TARGET  the recipient has their own event stream and must not appear here.
  MOVING      scale-normalised box displacement over a short window, with
              M-of-N hysteresis so one noisy box is not "motion".
  WELL IMAGED a recent best-shot exists, so we only fire when the person is
              actually photographable rather than a blur in a doorway.
  NOT A REPEAT per-track cooldown plus a global hourly cap, so a busy hallway
              cannot flood the outbox and crowd out a fall alert.

Per TRACK, not per home: two visitors are two subjects, and each gets its own
session, motion state and cooldown.
"""

import uuid
from collections import deque

from common import clock
from config.settings import settings
from rules.rule_context import RuleContext
from schemas.event import Event


class VisitorRule:
    """One motion-gated snapshot stream per non-recipient track."""

    def __init__(self) -> None:
        # (camera_id, track_id) -> state. Pruned against the live track set every
        # tick — a per-track dict keyed on a rising track_id is the leak shape
        # that already bit PostureBuffer and IdentityBuffer.
        self._boxes: dict[tuple, tuple] = {}        # last seen box
        self._moves: dict[tuple, deque] = {}        # recent moving/still verdicts
        self._last_snap: dict[tuple, float] = {}    # monotonic, per track
        self._hour: deque = deque()                 # global rate cap

    # ---- main ---------------------------------------------------------
    def evaluate(self, ctx: RuleContext) -> list[Event]:
        if not settings.visitor_snapshots_enabled:
            return []
        now = clock.now()
        events: list[Event] = []
        seen: set[tuple] = set()

        for camera_id, result in ctx.fresh_tracks(now).items():
            target_tid = ctx_target(ctx, camera_id)
            for track in result.tracks:
                if track.track_id == target_tid:
                    continue                       # the recipient is not a visitor
                ident = ctx.identities.get(camera_id, track.track_id)
                if ident is not None and ident.is_target:
                    continue                       # matched the recipient elsewhere

                key = (camera_id, track.track_id)
                seen.add(key)
                if not self._moving(key, track.bbox):
                    continue
                if not self._due(key):
                    continue
                if not self._well_imaged(ctx, camera_id, track.track_id):
                    continue
                self._mark(key)
                events.append(Event(
                    event_id=str(uuid.uuid4()),
                    event_type="visitor_motion_snapshot",
                    camera_id=camera_id,
                    room_name="",                  # filled by EventEnricher
                    recipient_id=None,             # a visitor has no identity
                    timestamp=now.isoformat(),
                    track_id=track.track_id,
                ))

        self._prune(seen)
        return events

    # ---- motion --------------------------------------------------------
    def _moving(self, key: tuple, bbox) -> bool:
        """Scale-normalised displacement with M-of-N hysteresis.

        Normalising by box HEIGHT is what makes one threshold work at both ends
        of a room: a person near the camera covers far more pixels per step than
        the same stride at the far wall. M-of-N rather than N-consecutive because
        real movement is intermittent — someone pauses mid-stride — while box
        jitter is independent tick to tick and cancels."""
        cur = ((bbox.x1 + bbox.x2) / 2.0, (bbox.y1 + bbox.y2) / 2.0)
        h = max(1.0, bbox.y2 - bbox.y1)
        prev = self._boxes.get(key)
        self._boxes[key] = cur

        win = self._moves.get(key)
        if win is None or win.maxlen != settings.visitor_motion_window:
            win = deque(win or (), maxlen=max(1, settings.visitor_motion_window))
            self._moves[key] = win
        if prev is None:
            return False                           # first sighting is not motion

        moved = (((cur[0] - prev[0]) ** 2 + (cur[1] - prev[1]) ** 2) ** 0.5) / h
        win.append(1 if moved >= settings.visitor_motion_frac else 0)
        return sum(win) >= settings.visitor_motion_hits

    # ---- rate limits ---------------------------------------------------
    def _due(self, key: tuple) -> bool:
        import time
        now = time.monotonic()
        if (now - self._last_snap.get(key, -1e9)) < settings.visitor_snapshot_cooldown_secs:
            return False
        # Global cap: a busy hallway must not crowd a fall alert out of the
        # outbox's sliding window.
        cutoff = now - 3600.0
        while self._hour and self._hour[0] < cutoff:
            self._hour.popleft()
        return len(self._hour) < settings.visitor_snapshots_per_hour

    def _mark(self, key: tuple) -> None:
        import time
        now = time.monotonic()
        self._last_snap[key] = now
        self._hour.append(now)

    # ---- quality -------------------------------------------------------
    @staticmethod
    def _well_imaged(ctx: RuleContext, camera_id: str, track_id: int) -> bool:
        """Only fire when a recent GOOD crop of this person exists.

        A snapshot of someone mid-stride is usually a smear, and a smear is not
        evidence of anything. The best-shot ring already scores every crop, so
        this costs a lookup: no buffer wired in means no gate, never a block."""
        shots = getattr(ctx, "best_shots", None)
        if shots is None:
            return True
        return shots.best(camera_id, track_id) is not None

    # ---- housekeeping --------------------------------------------------
    def _prune(self, seen: set) -> None:
        for store in (self._boxes, self._moves, self._last_snap):
            for key in [k for k in store if k not in seen]:
                store.pop(key, None)


def ctx_target(ctx: RuleContext, camera_id: str) -> int | None:
    """The locked recipient's track on this camera, if any. Read through the
    identity buffer so this rule needs no extra wiring — the TargetRegistry is
    not on RuleContext."""
    per = ctx.identities.get_all().get(camera_id, {})
    for tid, ident in per.items():
        if ident.is_target:
            return tid
    return None
