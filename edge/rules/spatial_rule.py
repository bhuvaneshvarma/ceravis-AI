from __future__ import annotations

"""
Area-aware rule for the care recipient:

  - area_transition : recipient moves between two NAMED functional areas
                      (kitchen -> living room). Debounced so boundary jitter
                      doesn't spam.
  - inactivity      : recipient stays in the SAME named area, alone, for
                      settings.inactivity_secs (default 30 min), re-emitted on
                      that interval (the "minimum activity snapshot" cadence).

Area membership comes from the person's FOOT point against the drawn zones
(ZoneResolver). Both events carry track_id so the enricher draws the box and
saves a snapshot. Requires zones; without them no area events fire (the
PostureRule still covers generic stillness).
"""

import uuid
from datetime import datetime, timezone

from common.zone_resolver import ZoneResolver
from config.settings import settings
from rules.rule_context import RuleContext
from schemas.event import Event


class SpatialRule:
    STABLE_TICKS = 2          # ticks settled in an area before a move counts

    def __init__(self, zone_resolver: ZoneResolver | None = None) -> None:
        self._zones = zone_resolver or ZoneResolver()
        # (camera_id, track_id) -> {area, stable, since, last_inact}
        self._st: dict[tuple[str, int], dict] = {}

    def evaluate(self, ctx: RuleContext) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)

        # "Alone" = no identified visitor anywhere right now.
        visitor_present = any(
            (ident is not None and not ident.is_target)
            for cam, res in ctx.tracks.get_all().items()
            for t in res.tracks
            for ident in [ctx.identities.get(cam, t.track_id)]
        )

        for camera_id, result in ctx.tracks.get_all().items():
            for track in result.tracks:
                ident = ctx.identities.get(camera_id, track.track_id)
                if not (ident and ident.is_target):     # recipient only
                    continue
                foot_x = (track.bbox.x1 + track.bbox.x2) / 2.0
                foot_y = track.bbox.y2
                area = self._zones.area_for(camera_id, foot_x, foot_y)
                key = (camera_id, track.track_id)
                st = self._st.get(key)
                if st is None:
                    self._st[key] = {"area": area, "stable": 1,
                                     "since": now, "last_inact": None}
                    continue

                if area == st["area"]:
                    st["stable"] += 1
                    self._maybe_inactivity(events, st, area, visitor_present,
                                           camera_id, track.track_id, ident, now)
                    continue

                # area changed
                prev_area, prev_stable = st["area"], st["stable"]
                st.update({"area": area, "stable": 1,
                           "since": now, "last_inact": None})
                if prev_stable >= self.STABLE_TICKS and prev_area and area:
                    events.append(self._evt(
                        "area_transition", camera_id, track.track_id, ident, now,
                        detail=f"{prev_area} → {area}"))
        return events

    # ----------------------------------------------------------------
    def _maybe_inactivity(self, events, st, area, visitor_present,
                          camera_id, track_id, ident, now) -> None:
        if area is None or visitor_present:
            return                                  # only when alone in a known area
        held = (now - st["since"]).total_seconds()
        if held < settings.inactivity_secs:
            return
        last = st["last_inact"]
        if last is not None and (now - last).total_seconds() < settings.inactivity_secs:
            return                                  # already emitted this interval
        st["last_inact"] = now
        events.append(self._evt(
            "inactivity", camera_id, track_id, ident, now,
            detail=f"no area change for {int(held // 60)} min in {area}"))

    @staticmethod
    def _evt(event_type, camera_id, track_id, ident, now, detail=None) -> Event:
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            camera_id=camera_id,
            room_name="",
            recipient_id=ident.recipient_id,
            timestamp=now.isoformat(),
            track_id=track_id,
            detail=detail,
        )
