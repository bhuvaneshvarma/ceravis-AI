from __future__ import annotations

import uuid
from datetime import datetime, timezone

from schemas.event import Event
from rules.rule_context import RuleContext


class VisitorRule:
    """
    Fires when a tracked person is NOT recognized as an enrolled recipient.

    Cooldown per (camera, track) so we don't spam.
    """

    COOLDOWN_SECS = 60

    def __init__(self) -> None:
        self._last_fired: dict[tuple[str, int], datetime] = {}

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        for camera_id, track_result in ctx.tracks.get_all().items():
            for track in track_result.tracks:
                identity = ctx.identities.get(camera_id, track.track_id)
                if identity is None or identity.is_target:
                    continue
                key = (camera_id, track.track_id)
                last = self._last_fired.get(key)
                if last and (now - last).total_seconds() < self.COOLDOWN_SECS:
                    continue
                self._last_fired[key] = now
                events.append(
                    Event(
                        event_id=str(uuid.uuid4()),
                        event_type="visitor",
                        camera_id=camera_id,
                        room_name="",
                        timestamp=now.isoformat(),
                    )
                )
        return events
