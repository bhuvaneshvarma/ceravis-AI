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
  NOT A REPEAT at most ONE visitor snapshot per `visitor_snapshot_interval_secs`
              for the WHOLE home (default 60 s), however many people are
              moving and however often the tracker re-numbers them. A per-track
              limit cannot bound volume: a busy room or one ID switch mints a
              fresh "never snapped" track and the burst starts over.

Motion is judged per TRACK (two visitors are two subjects with their own motion
state), but the snapshot budget is per HOME. When several visitors are eligible
in the same tick, the one photographed least recently takes the slot, so two
people walking around alternate instead of one of them being captured forever.

NIGHT VISION: on an infrared camera the recipient may simply be unrecognised,
so while the recipient is located NOWHERE an unidentified person there is held
rather than reported — the care recipient walking to the bathroom at 2 am is not
a visitor. The moment the recipient is placed (on any camera, by any lock
basis), everyone else is a visitor again, in the dark as in daylight.
"""

import time
import uuid
from collections import deque

from common import clock
from config.settings import settings
from ingestion import illumination
from rules.rule_context import RuleContext
from schemas.event import Event


class VisitorRule:
    """Motion-gated snapshots of non-recipients, one per interval per home."""

    def __init__(self) -> None:
        # (camera_id, track_id) -> state. Pruned against the live track set every
        # tick — a per-track dict keyed on a rising track_id is the leak shape
        # that already bit PostureBuffer and IdentityBuffer.
        self._boxes: dict[tuple, tuple] = {}        # last seen box
        self._moves: dict[tuple, deque] = {}        # recent moving/still verdicts
        self._last_snap: dict[tuple, float] = {}    # monotonic, per track (fairness)
        self._first_seen: dict[tuple, float] = {}   # monotonic, per track (identity grace)
        self._last_any: float = float("-inf")       # monotonic, home-wide budget

    # ---- main ---------------------------------------------------------
    def evaluate(self, ctx: RuleContext) -> list[Event]:
        if not settings.visitor_snapshots_enabled:
            return []
        now = clock.now()
        candidates: list[tuple] = []
        seen: set[tuple] = set()
        located: list[bool] = []                  # lazily: recipient placed anywhere?

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
                self._first_seen.setdefault(key, time.monotonic())
                if not self._moving(key, track.bbox):
                    continue
                if self._identity_hold(ctx, key):
                    continue                       # could be the recipient arriving
                if self._night_hold(ctx, camera_id, now, located):
                    continue                       # could be the recipient, in the dark
                if not self._well_imaged(ctx, camera_id, track.track_id):
                    continue
                candidates.append(key)

        self._prune(seen)
        if not candidates or not self._due():
            return []
        # Fairness: the visitor photographed least recently (never = first).
        camera_id, track_id = min(
            candidates, key=lambda k: self._last_snap.get(k, float("-inf")))
        self._mark((camera_id, track_id))
        return [Event(
            event_id=str(uuid.uuid4()),
            event_type="visitor_motion_snapshot",
            camera_id=camera_id,
            room_name="",                          # filled by EventEnricher
            recipient_id=None,                     # a visitor has no identity
            timestamp=now.isoformat(),
            track_id=track_id,
        )]

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

    # ---- identity grace ------------------------------------------------
    def _identity_hold(self, ctx: RuleContext, key: tuple) -> bool:
        """Hold this track's FIRST snapshot for a beat while the recipient is
        being re-found across cameras — a freshly-appeared unidentified person
        could be the recipient walking into this room, and ReID (running faster
        than this rule) will claim them within the grace. Only bites during an
        active search and only for the first grace-seconds of a track's life, so
        ordinary visitors are unaffected. No registry wired in -> never holds."""
        reg = getattr(ctx, "target_registry", None)
        if reg is None:
            return False
        try:
            if not reg.searching():
                return False
        except Exception:
            return False
        first = self._first_seen.get(key)
        if first is None:
            return True
        return (time.monotonic() - first) < settings.visitor_identity_grace_secs

    @staticmethod
    def _night_hold(ctx: RuleContext, camera_id: str, now, located: list) -> bool:
        """On an infrared camera, hold an unidentified person while the
        recipient is located nowhere — they may BE the recipient, unrecognised
        in the dark. Evaluated at most once per tick (`located` caches it). If
        the recipient's whereabouts cannot be read, nothing is held."""
        if not settings.visitor_ir_hold or not illumination.is_ir(camera_id):
            return False
        if not located:
            find = getattr(ctx, "find_recipient", None)
            try:
                located.append(find is None or find(now) is not None)
            except Exception:
                located.append(True)
        return not located[0]

    # ---- rate limit ----------------------------------------------------
    def _due(self) -> bool:
        """ONE home-wide budget: a snapshot at most every interval seconds.
        Bounds volume absolutely, so a busy hallway can never crowd a fall
        alert out of the outbox's sliding window."""
        return (time.monotonic() - self._last_any) >= settings.visitor_snapshot_interval_secs

    def _mark(self, key: tuple) -> None:
        now = time.monotonic()
        self._last_snap[key] = now                 # fairness between visitors
        self._last_any = now                       # the home-wide budget

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
        for store in (self._boxes, self._moves, self._last_snap, self._first_seen):
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
