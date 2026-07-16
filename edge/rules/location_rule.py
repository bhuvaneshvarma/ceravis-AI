from __future__ import annotations

"""
One location mechanism for the recipient, two depths of the same signal:

  room (coarse) — WHICH CAMERA sees them. Cameras map to rooms, so a debounced
                  move to a camera in a different room = `room_transition`
                  ("kitchen → living room"). The previous room stays valid as
                  the origin across sighting gaps (an uncovered hallway) up to
                  settings.room_transition_max_gap_secs.
  area (fine)   — WHERE INSIDE that camera their feet are, against the drawn
                  zones = `area_transition` ("stove → dining"). Needs zones;
                  without them only the room depth fires.

Replaces the separate SpatialRule + RoomTransitionRule: same source signal
(the shared recipient resolver), one debounced tracker, hierarchical detail.
Long-dwell welfare checks live in StillnessRule alone.
"""

import uuid
from datetime import datetime

from common import clock
from common.zone_resolver import ZoneResolver
from config.settings import settings
from configuration.camera_config import CameraConfig
from rules.rule_context import RuleContext
from schemas.event import Event


class LocationRule:
    STABLE_TICKS = 2       # ticks settled before a room/area move counts
    FRESH_SECS = 5.0       # sighting freshness (see common.freshness)

    def __init__(self, camera_config: CameraConfig | None = None,
                 zone_resolver: ZoneResolver | None = None) -> None:
        self._cams = camera_config or CameraConfig()
        self._zones = zone_resolver or ZoneResolver()
        # room depth (camera)
        self._cam_cand: str | None = None
        self._cam_ticks = 0
        self._cur_cam: str | None = None
        self._cur_room: str = ""
        self._last_seen: datetime | None = None
        # area depth (zone inside the settled camera)
        self._area_cand: str | None = None
        self._area_ticks = 0
        self._cur_area: str | None = None

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        now = clock.now()
        s = ctx.find_recipient(now, self.FRESH_SECS)
        if s is None:
            self._cam_cand, self._cam_ticks = None, 0
            self._area_cand, self._area_ticks = None, 0
            # Out of view: hold the last room as a transition origin for a
            # while (walking through an uncovered hallway), then forget it so
            # a much later sighting starts fresh with no transition.
            if (self._cur_cam is not None and self._last_seen is not None
                    and (now - self._last_seen).total_seconds()
                    > settings.room_transition_max_gap_secs):
                self._cur_cam, self._cur_room, self._cur_area = None, "", None
            return []

        self._last_seen = now
        events: list[Event] = []
        cam = s.camera_id

        # ---- room depth: debounced camera change ---------------------
        if cam == self._cam_cand:
            self._cam_ticks += 1
        else:
            self._cam_cand, self._cam_ticks = cam, 1
        if self._cam_ticks >= self.STABLE_TICKS and cam != self._cur_cam:
            room = self._room_of(cam)
            prev_cam, prev_room = self._cur_cam, self._cur_room
            self._cur_cam, self._cur_room = cam, room
            # area depth restarts per camera (zones are camera-local)
            self._cur_area, self._area_cand, self._area_ticks = None, None, 0
            if (prev_cam is not None and prev_room and room
                    and prev_room.strip().lower() != room.strip().lower()):
                events.append(self._evt("room_transition", s, now,
                                        f"{prev_room} → {room}"))

        # ---- area depth: zones inside the settled camera -------------
        if cam == self._cur_cam:
            foot_x = (s.track.bbox.x1 + s.track.bbox.x2) / 2.0
            foot_y = s.track.bbox.y2
            area = self._zones.area_for(cam, foot_x, foot_y)
            if area == self._area_cand:
                self._area_ticks += 1
            else:
                self._area_cand, self._area_ticks = area, 1
            if self._area_ticks >= self.STABLE_TICKS and area != self._cur_area:
                prev_area, self._cur_area = self._cur_area, area
                if prev_area and area:
                    events.append(self._evt("area_transition", s, now,
                                            f"{prev_area} → {area}"))
        return events

    # ----------------------------------------------------------------
    def _room_of(self, camera_id: str) -> str:
        try:
            cam = self._cams.get_by_id(camera_id)
        except Exception:
            cam = None
        return (cam.room_name or "") if cam is not None else ""

    @staticmethod
    def _evt(event_type: str, s, now: datetime, detail: str) -> Event:
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            camera_id=s.camera_id,
            room_name="",            # filled by EventEnricher (camera config)
            recipient_id=s.identity.recipient_id,
            timestamp=now.isoformat(),
            track_id=s.track.track_id,   # lets the enricher snapshot the frame
            detail=detail,
        )
