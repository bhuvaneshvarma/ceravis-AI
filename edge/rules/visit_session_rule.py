from __future__ import annotations

"""
Home-level visitor visit sessions (replaces the noisy per-track VisitorRule):

  - visitor_arrival   : a visitor (any tracked person who is NOT the recipient)
                        appears while no visit is active.
  - visitor_present   : periodic "still here" beat during a visit (the mid-visit
                        snapshot cadence).
  - visitor_departure : no visitor anywhere for settings.visit_absence_secs ->
                        close the visit, report onsite duration.

Increment 1 is identity-agnostic: it tracks WHETHER a visitor is present, not
WHICH one — that (matching against a registered-visitor body gallery) is the
next increment. Events carry the visitor's track_id so the enricher boxes them
and saves a snapshot.
"""

import uuid
from datetime import datetime, timezone

from config.settings import settings
from rules.rule_context import RuleContext
from schemas.event import Event


class VisitSessionRule:
    def __init__(self) -> None:
        self._active = False
        self._arrival: datetime | None = None
        self._last_seen: datetime | None = None
        self._last_mid: datetime | None = None
        self._last_cam: str = ""

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        # First visitor (non-target) track anywhere — used for the snapshot box.
        v_cam, v_track = None, None
        for cam, res in ctx.tracks.get_all().items():
            for t in res.tracks:
                ident = ctx.identities.get(cam, t.track_id)
                if ident is not None and not ident.is_target:
                    v_cam, v_track = cam, t
                    break
            if v_track is not None:
                break

        if v_track is not None:                          # a visitor is present
            self._last_seen = now
            self._last_cam = v_cam
            if not self._active:
                self._active = True
                self._arrival = now
                self._last_mid = now
                events.append(self._evt("visitor_arrival", v_cam, v_track.track_id, now))
            elif self._last_mid is None or \
                    (now - self._last_mid).total_seconds() >= settings.visit_mid_secs:
                self._last_mid = now
                events.append(self._evt("visitor_present", v_cam, v_track.track_id, now))
        elif self._active and self._last_seen is not None and \
                (now - self._last_seen).total_seconds() >= settings.visit_absence_secs:
            mins = int((self._last_seen - self._arrival).total_seconds() // 60) \
                if self._arrival else 0
            self._active = False
            self._arrival = None
            events.append(self._evt("visitor_departure", self._last_cam, None, now,
                                    detail=f"visit lasted {mins} min"))
        return events

    @staticmethod
    def _evt(event_type, camera_id, track_id, now, detail=None) -> Event:
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            camera_id=camera_id or "",
            room_name="",
            timestamp=now.isoformat(),
            track_id=track_id,
            detail=detail,
        )
