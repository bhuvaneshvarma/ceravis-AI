from __future__ import annotations

"""
Inactivity burst: when the care recipient hasn't moved for a long time, take a
snapshot every minute for a fixed window — a welfare check on prolonged stillness.

Default: 30 min of no movement → one `inactivity_snapshot` per minute for 15 min
(15 snapshots). "Movement" = the recipient's box centre shifting more than a
fraction of its own height (scale-invariant); changing camera/room also counts.
These are snapshot-ONLY (no alert); the cloud publisher routes them to
saveSnapshot. Resets the moment the recipient moves again.
"""

import uuid
from datetime import datetime, timezone

from config.settings import settings
from schemas.event import Event
from rules.rule_context import RuleContext


class InactivitySnapshotRule:
    def __init__(self) -> None:
        # one recipient → one state
        self._cam: str | None = None
        self._anchor: tuple[float, float, float] | None = None   # cx, cy, h
        self._still_since: datetime | None = None
        self._snaps_fired = 0
        self._last_snap_at: datetime | None = None

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        now = datetime.now(timezone.utc)
        target = self._find_target(ctx)          # (camera_id, track_id, cx, cy, h, rid)
        if target is None:
            self._reset()
            return []

        camera_id, track_id, cx, cy, h, rid = target
        if self._moved(camera_id, cx, cy, h):
            self._cam = camera_id
            self._anchor = (cx, cy, h)
            self._still_since = now
            self._snaps_fired = 0
            self._last_snap_at = None
            return []

        # Still — has it been long enough to start the burst?
        if self._still_since is None:
            self._still_since = now
            return []
        if (now - self._still_since).total_seconds() < settings.inactivity_snapshot_after_secs:
            return []
        if self._snaps_fired >= settings.inactivity_snapshot_count:
            return []
        if (self._last_snap_at is not None and (now - self._last_snap_at).total_seconds()
                < settings.inactivity_snapshot_interval_secs):
            return []

        self._snaps_fired += 1
        self._last_snap_at = now
        return [Event(
            event_id=str(uuid.uuid4()),
            event_type="inactivity_snapshot",
            camera_id=camera_id,
            room_name="",
            recipient_id=rid,
            timestamp=now.isoformat(),
            track_id=track_id,
            detail=f"{self._snaps_fired}/{settings.inactivity_snapshot_count}",
        )]

    # ----------------------------------------------------------------
    def _find_target(self, ctx: RuleContext):
        for camera_id, result in ctx.tracks.get_all().items():
            for t in result.tracks:
                ident = ctx.identities.get(camera_id, t.track_id)
                if ident and ident.is_target:
                    b = t.bbox
                    cx, cy = (b.x1 + b.x2) / 2.0, (b.y1 + b.y2) / 2.0
                    return (camera_id, t.track_id, cx, cy, max(b.height, 1.0),
                            ident.recipient_id)
        return None

    def _moved(self, camera_id: str, cx: float, cy: float, h: float) -> bool:
        if self._anchor is None or self._cam != camera_id:
            return True                          # first sighting / changed camera
        ax, ay, _ = self._anchor
        dist = ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5
        return dist > settings.inactivity_snapshot_move_frac * h

    def _reset(self) -> None:
        self._cam = None
        self._anchor = None
        self._still_since = None
        self._snaps_fired = 0
        self._last_snap_at = None
