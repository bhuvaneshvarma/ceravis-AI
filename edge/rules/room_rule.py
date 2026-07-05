from __future__ import annotations

"""
Room-to-room transition for the care recipient.

The SpatialRule reads zone areas WITHIN one camera; this rule watches which
CAMERA the recipient is on. Cameras map to rooms, so a debounced move from a
camera in one room to a camera in another IS the recipient changing rooms
("kitchen → living room") — including across the sighting gap of an uncovered
hallway (the previous room stays valid as the transition origin for
settings.room_transition_max_gap_secs).

Fresh tracks only: with active-camera-only focus the idle cameras' buffers go
stale rather than empty, so per-camera results older than FRESH_SECS are
ignored. Debounced over STABLE_TICKS so one ReID flicker onto another camera
never emits a phantom move.
"""

import uuid
from datetime import datetime, timezone

from config.settings import settings
from configuration.camera_config import CameraConfig
from rules.rule_context import RuleContext
from schemas.event import Event


class RoomTransitionRule:
    STABLE_TICKS = 2       # ticks on the same camera before the move counts
    FRESH_SECS = 5.0       # ignore stale per-camera track results

    def __init__(self, camera_config: CameraConfig | None = None) -> None:
        self._cams = camera_config or CameraConfig()
        self._cand: str | None = None      # camera being debounced
        self._cand_ticks = 0
        self._cur_cam: str | None = None   # settled camera the recipient is on
        self._cur_room: str = ""
        self._last_seen: datetime | None = None

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        now = datetime.now(timezone.utc)
        seen = self._recipient_sighting(ctx, now)
        if seen is None:
            self._cand, self._cand_ticks = None, 0
            # Out of view: hold the last room as the transition origin for a
            # while (walking through an uncovered hallway), then forget it so
            # a much later sighting starts fresh with no transition.
            if (self._cur_cam is not None and self._last_seen is not None
                    and (now - self._last_seen).total_seconds()
                    > settings.room_transition_max_gap_secs):
                self._cur_cam, self._cur_room = None, ""
            return []

        camera_id, track_id, ident = seen
        self._last_seen = now
        if camera_id == self._cand:
            self._cand_ticks += 1
        else:
            self._cand, self._cand_ticks = camera_id, 1
        if self._cand_ticks < self.STABLE_TICKS or camera_id == self._cur_cam:
            return []

        room = self._room_of(camera_id)
        prev_cam, prev_room = self._cur_cam, self._cur_room
        self._cur_cam, self._cur_room = camera_id, room
        if (prev_cam is None or not prev_room or not room
                or prev_room.strip().lower() == room.strip().lower()):
            return []        # first sighting / same room seen by another camera
        return [Event(
            event_id=str(uuid.uuid4()),
            event_type="room_transition",
            camera_id=camera_id,
            room_name="",    # filled by EventEnricher (camera config)
            recipient_id=ident.recipient_id,
            timestamp=now.isoformat(),
            track_id=track_id,   # lets the enricher snapshot the arrival frame
            detail=f"{prev_room} → {room}",
        )]

    # ----------------------------------------------------------------
    def _recipient_sighting(self, ctx: RuleContext, now: datetime):
        """(camera_id, track_id, identity) of the highest-confidence recipient
        track in a FRESH per-camera result, or None when out of view."""
        best = None
        for camera_id, result in ctx.fresh_tracks(now, self.FRESH_SECS).items():
            for track in result.tracks:
                ident = ctx.identities.get(camera_id, track.track_id)
                if not (ident and ident.is_target):
                    continue
                if best is None or ident.confidence > best[2].confidence:
                    best = (camera_id, track.track_id, ident)
        return best

    def _room_of(self, camera_id: str) -> str:
        try:
            cam = self._cams.get_by_id(camera_id)
        except Exception:
            cam = None
        return (cam.room_name or "") if cam is not None else ""
