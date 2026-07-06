from __future__ import annotations

"""
Posture-transition events for the recipient — the rule-engine face of the
posture stream the PostureTracker already maintains.

Emits one snapshot-only event per CONFIRMED posture change:
  'standing_up'     : SITTING -> STANDING
  'sitting_down'    : STANDING -> SITTING
  'walking_started' : * -> WALKING
  'walking_stopped' : WALKING -> STANDING

This rule only narrates the transitions. The other depths of the same
signals live in their single owners: long-dwell welfare (frozen skeleton /
unchanged posture) is StillnessRule's job, location moves are
LocationRule's, and falls belong to the fall FSM via FallRule.
"""

import uuid
from datetime import datetime, timezone

from pose.posture_classifier import Posture
from schemas.event import Event
from rules.rule_context import RuleContext


class PostureRule:
    def __init__(self) -> None:
        # (camera_id, track_id) -> current confirmed posture
        self._state: dict[tuple[str, int], Posture] = {}

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        # The shared resolver answers "where is the recipient" — recipient_id
        # is set on each event so the cloud publisher's recipient gate passes.
        s = ctx.find_recipient(now)
        if s is None:
            return []
        camera_id, track_id = s.camera_id, s.track.track_id
        key = (camera_id, track_id)
        prev = self._state.get(key)
        if prev == s.posture:
            return []
        self._state[key] = s.posture
        if prev is not None:
            self._on_transition(events, camera_id, track_id, prev, s.posture,
                                now, s.identity.recipient_id)
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
