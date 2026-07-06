from __future__ import annotations

"""
Posture-transition rule: emits events when a tracked person stays in
a posture for a meaningful duration.

Events emitted:
  - 'prolonged_sitting'  : SITTING continuously for > sitting_min_secs
  - 'standing_up'        : SITTING -> STANDING transition
  - 'walking_started'    : * -> WALKING transition
  - 'no_movement'        : STANDING with no centroid motion > 30 s
                           (posture-aware stillness; the SpatialRule handles
                           area-aware inactivity over much longer windows)

These are infrequent (1 Hz tick), so they don't tax the Orin Nano.
"""

import uuid
from collections import defaultdict
from datetime import datetime, timezone

from config.settings import settings
from pose.posture_classifier import Posture
from schemas.event import Event
from rules.rule_context import RuleContext


class PostureRule:
    NO_MOVE_SECS = 30.0

    def __init__(self) -> None:
        # (camera_id, track_id) -> (current_posture, entered_at)
        self._state: dict[tuple[str, int], tuple[Posture, datetime]] = {}
        self._fired: dict[tuple[str, int], dict[str, datetime]] = defaultdict(dict)

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        # The shared resolver answers "where is the recipient" — this rule
        # only cares about the CR's posture stream (recipient_id set so the
        # cloud publisher's recipient gate passes).
        s = ctx.find_recipient(now)
        if s is None:
            return []
        camera_id, track_id = s.camera_id, s.track.track_id
        rid = s.identity.recipient_id
        key = (camera_id, track_id)
        prev = self._state.get(key)

        # Transition
        if prev is None or prev[0] != s.posture:
            if prev is not None:
                self._on_transition(events, camera_id, track_id,
                                    prev[0], s.posture, now, rid)
            self._state[key] = (s.posture, now)
            self._fired[key].clear()
            return events

        # Same posture — check for duration thresholds
        in_state = (now - prev[1]).total_seconds()
        self._duration_events(events, camera_id, track_id, s.posture,
                              in_state, now, key, rid)
        return events

    # ----------------------------------------------------------------
    def _on_transition(
        self,
        events: list[Event],
        camera_id: str,
        track_id: int,
        old: Posture,
        new: Posture,
        now: datetime,
        rid: str | None,
    ) -> None:
        # Each maps to a snapshot-only event; the publisher renders the arrow.
        if old == Posture.SITTING and new == Posture.STANDING:
            events.append(self._make("standing_up", camera_id, now, track_id, rid))
        elif old == Posture.STANDING and new == Posture.SITTING:
            events.append(self._make("sitting_down", camera_id, now, track_id, rid))
        elif new == Posture.WALKING and old != Posture.WALKING:
            events.append(self._make("walking_started", camera_id, now, track_id, rid))
        elif old == Posture.WALKING and new == Posture.STANDING:
            events.append(self._make("walking_stopped", camera_id, now, track_id, rid))

    def _duration_events(
        self,
        events: list[Event],
        camera_id: str,
        track_id: int,
        posture: Posture,
        in_state: float,
        now: datetime,
        key: tuple[str, int],
        rid: str | None,
    ) -> None:
        already = self._fired[key]

        if posture == Posture.SITTING and in_state > settings.sitting_min_secs * 12:
            # 12x sitting_min_secs is the "prolonged" boundary — default 60s
            if "prolonged_sitting" not in already:
                events.append(self._make("prolonged_sitting", camera_id, now, track_id, rid))
                already["prolonged_sitting"] = now

        if posture == Posture.STANDING and in_state > self.NO_MOVE_SECS:
            if "no_movement" not in already:
                events.append(self._make("no_movement", camera_id, now, track_id, rid))
                already["no_movement"] = now

    @staticmethod
    def _make(event_type: str, camera_id: str, now: datetime,
              track_id: int | None = None, rid: str | None = None) -> Event:
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            camera_id=camera_id,
            room_name="",
            recipient_id=rid,
            timestamp=now.isoformat(),
            track_id=track_id,
        )
