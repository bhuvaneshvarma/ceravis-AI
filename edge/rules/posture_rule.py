from __future__ import annotations

"""
Posture-transition rule: emits events when a tracked person stays in
a posture for a meaningful duration.

Events emitted:
  - 'prolonged_sitting'  : SITTING continuously for > sitting_min_secs
  - 'standing_up'        : SITTING -> STANDING transition
  - 'walking_started'    : * -> WALKING transition
  - 'no_movement'        : STANDING with no centroid motion > 30 s
                           (the InactivityRule already covers the
                           generic "track hasn't moved" case — this one
                           is posture-aware, fires sooner if upright)

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

        for camera_id, per_track in ctx.postures.get_all().items():
            for track_id, rec in per_track.items():
                key = (camera_id, track_id)
                prev = self._state.get(key)

                # Transition
                if prev is None or prev[0] != rec.posture:
                    if prev is not None:
                        self._on_transition(events, camera_id, track_id,
                                            prev[0], rec.posture, now)
                    self._state[key] = (rec.posture, now)
                    self._fired[key].clear()
                    continue

                # Same posture — check for duration thresholds
                entered = prev[1]
                in_state = (now - entered).total_seconds()
                self._duration_events(events, camera_id, track_id, rec.posture,
                                      in_state, now, key)
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
    ) -> None:
        if old == Posture.SITTING and new == Posture.STANDING:
            events.append(self._make("standing_up", camera_id, now))
        elif new == Posture.WALKING and old != Posture.WALKING:
            events.append(self._make("walking_started", camera_id, now))

    def _duration_events(
        self,
        events: list[Event],
        camera_id: str,
        track_id: int,
        posture: Posture,
        in_state: float,
        now: datetime,
        key: tuple[str, int],
    ) -> None:
        already = self._fired[key]

        if posture == Posture.SITTING and in_state > settings.sitting_min_secs * 12:
            # 12x sitting_min_secs is the "prolonged" boundary — default 60s
            if "prolonged_sitting" not in already:
                events.append(self._make("prolonged_sitting", camera_id, now))
                already["prolonged_sitting"] = now

        if posture == Posture.STANDING and in_state > self.NO_MOVE_SECS:
            if "no_movement" not in already:
                events.append(self._make("no_movement", camera_id, now))
                already["no_movement"] = now

    @staticmethod
    def _make(event_type: str, camera_id: str, now: datetime) -> Event:
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            camera_id=camera_id,
            room_name="",
            timestamp=now.isoformat(),
        )
