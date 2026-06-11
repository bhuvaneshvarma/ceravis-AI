from __future__ import annotations

import uuid
from datetime import datetime, timezone

from schemas.event import Event
from rules.rule_context import RuleContext


class InactivityRule:
    """
    Emits an 'inactivity' event when the target recipient hasn't moved
    by more than MOVE_PIXELS for INACTIVITY_SECS.
    """

    MOVE_PIXELS = 30
    INACTIVITY_SECS = 600  # 10 min

    def __init__(self) -> None:
        # (camera_id, track_id) -> (last_centroid, last_motion_ts)
        self._last_motion: dict[tuple[str, int], tuple[tuple[float, float], datetime]] = {}
        self._fired: set[tuple[str, int]] = set()

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        for camera_id, track_result in ctx.tracks.get_all().items():
            for track in track_result.tracks:
                key = (camera_id, track.track_id)
                cx = (track.bbox.x1 + track.bbox.x2) / 2
                cy = (track.bbox.y1 + track.bbox.y2) / 2
                prev = self._last_motion.get(key)
                if prev is None:
                    self._last_motion[key] = ((cx, cy), now)
                    continue
                (px, py), pts = prev
                if ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 > self.MOVE_PIXELS:
                    self._last_motion[key] = ((cx, cy), now)
                    self._fired.discard(key)
                elif (now - pts).total_seconds() > self.INACTIVITY_SECS \
                        and key not in self._fired:
                    self._fired.add(key)
                    events.append(
                        Event(
                            event_id=str(uuid.uuid4()),
                            event_type="inactivity",
                            camera_id=camera_id,
                            room_name="",
                            timestamp=now.isoformat(),
                        )
                    )
        return events
