from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import cv2

from common import clock, event_snapshots
from common.zone_resolver import ZoneResolver
from config.settings import settings
from configuration.account_config import account_recipient
from configuration.camera_config import CameraConfig
from rules.rule_context import RuleContext
from schemas.event import Event


logger = logging.getLogger("rules")

# event_type -> (severity, human title)
# Events that are ABOUT somebody who is not the care recipient. Defined here,
# beside the event vocabulary, and imported by the cloud publisher — two lists
# of the same thing drift the moment one is edited.
NON_RECIPIENT_TYPES = {"visitor_motion_snapshot"}

_ALERT_MAP: dict[str, tuple[str, str]] = {
    "fall": ("critical", "Fall detected"),
    "lying_down": ("info", "Lying down"),
    "standing_up": ("info", "Stood up"),
    "sitting_down": ("info", "Sat down"),
    "walking_started": ("info", "Started walking"),
    "walking_stopped": ("info", "Stopped walking"),
    "no_motion": ("critical", "No motion"),
    "no_motion_snapshot": ("info", "No motion"),
    "no_transition_snapshot": ("info", "No transition"),
    "visitor_motion_snapshot": ("info", "Visitor moving"),
    "area_transition": ("info", "Moved area"),
    "room_transition": ("info", "Changed room"),
}


class EventEnricher:
    """
    Fills room/area, an annotated snapshot, and operator-facing alert fields
    (severity/title/message) on each event before it is published/stored.

    Zone-aware fall: a 'fall' whose foot point lands in a rest zone (bed/couch/…)
    is re-labelled 'lying_down' (info) instead of a critical fall — so resting
    on the bed never raises an alarm, but lying on the floor does.
    """

    def __init__(
        self,
        camera_config: CameraConfig | None = None,
        zone_resolver: ZoneResolver | None = None,
    ) -> None:
        self._cams = camera_config or CameraConfig()
        self._zones = zone_resolver or ZoneResolver()
        self._events_root = event_snapshots.events_root()
        self._rest_kw = [k.strip().lower()
                         for k in settings.rest_zone_keywords.split(",") if k.strip()]
        self._name_cache: dict[str, str] = {}      # recipient_id -> full_name
        self._name_cache_at = 0.0

    # ---- public ------------------------------------------------------
    def enrich(self, event: Event, ctx: RuleContext) -> Event:
        cam = self._cams.get_by_id(event.camera_id)
        if cam is not None and not event.room_name:
            event.room_name = cam.room_name

        bbox = self._track_bbox(event, ctx)
        area = None
        if bbox is not None:
            foot_x = (bbox[0] + bbox[2]) / 2.0
            foot_y = bbox[3]
            area = self._zones.area_for(event.camera_id, foot_x, foot_y)
            event.zone_name = area

        # Zone-aware fall: resting in a bed/couch zone is not a fall.
        if event.event_type == "fall" and self._is_rest_zone(area):
            event.event_type = "lying_down"

        severity, title = _ALERT_MAP.get(
            event.event_type, ("info", event.event_type.replace("_", " ")))
        event.severity = severity
        event.title = title
        loc = event.room_name + (f" / {area}" if area else "")
        # A visitor event has no recipient_id BY DESIGN, so resolving a name
        # would print "person" — or worse, the recipient's name, implying the
        # snapshot is of them.
        who = ("a visitor" if event.event_type in NON_RECIPIENT_TYPES
               else (self._recipient_name(event.recipient_id) or "person"))
        event.co_present = self._co_present(event, ctx)
        event.message = f"{title} — {who}" + (f" in {loc}" if loc.strip() else "")
        if event.co_present:
            event.message += f" · {event.co_present}"
        if event.detail:
            event.message += f" · {event.detail}"

        self._write_snapshot(event, ctx, bbox, area)
        return event

    def _co_present(self, event, ctx) -> str | None:
        """Who ELSE was in this frame, as a phrase — or None when alone.

        A visitor walking past while the recipient stands up produces two
        events about ONE frame, and describing them separately reads as two
        unrelated things happening. Naming the co-presence on each turns them
        into one legible fact: "Ravi stood up, and a visitor was there too."

        Counts fresh tracks only. An idle camera keeps its last TrackResult
        forever, so an unchecked read would report a visitor who left an hour
        ago as standing in the room."""
        tracks = getattr(ctx, "tracks", None)
        idents = getattr(ctx, "identities", None)
        if tracks is None or idents is None:
            return None
        try:
            fresh = ctx.fresh_tracks(clock.now())
        except Exception:
            return None
        result = fresh.get(event.camera_id)
        if result is None:
            return None

        visitors, target_name = 0, None
        for t in result.tracks:
            if t.track_id == event.track_id:
                continue                      # the subject is not their own company
            ident = idents.get(event.camera_id, t.track_id)
            if ident is not None and ident.is_target:
                target_name = self._recipient_name(ident.recipient_id) or "the recipient"
            else:
                visitors += 1

        parts = []
        if target_name:
            parts.append(target_name)
        if visitors == 1:
            parts.append("a visitor")
        elif visitors > 1:
            parts.append(f"{visitors} visitors")
        return ("with " + " and ".join(parts)) if parts else None


    # ---- helpers -----------------------------------------------------
    def _track_bbox(self, event: Event, ctx: RuleContext):
        if event.track_id is None:
            return None
        result = ctx.tracks.get(event.camera_id)
        if result is None:
            return None
        for t in result.tracks:
            if t.track_id == event.track_id:
                return (t.bbox.x1, t.bbox.y1, t.bbox.x2, t.bbox.y2)
        return None

    def _is_rest_zone(self, area: str | None) -> bool:
        # Whole-word match so "bedside floor" / "bedroom floor" (a fall spot) is
        # NOT mistaken for a "bed" rest zone — only an actual bed/couch area is.
        if not area:
            return False
        words = set(re.findall(r"[a-z]+", area.lower()))
        return any(kw in words for kw in self._rest_kw)

    def _recipient_name(self, rid: str | None) -> str | None:
        """Resolve a recipient_id to the saved full name (for snapshot label +
        alert text). The care recipient IS the verified account holder
        (configuration.account_config.account_recipient) — no recipients.json.
        Cached briefly; falls back to the id if unresolved, None if no rid."""
        if not rid:
            return None
        now = time.monotonic()
        if now - self._name_cache_at > 10.0:
            try:
                r = account_recipient()
                self._name_cache = ({r["recipient_id"]: r["full_name"]} if r
                                    else {})
            except Exception:
                logger.exception("recipient name lookup failed")
            self._name_cache_at = now
        return self._name_cache.get(rid) or rid

    def _write_snapshot(self, event: Event, ctx: RuleContext, bbox, area) -> None:
        # Save a CLEAN frame (no overlay) — the alert message carries the
        # severity/title/room/area/recipient/time. The app server renders its own
        # UI; a raw frame is the most useful evidence and the smallest file.
        try:
            fd = ctx.frames.get(event.camera_id)
            if fd is None:
                return
            day = clock.now().strftime("%Y-%m-%d")
            rel = Path(settings.device_id) / day / f"{event.event_id}.jpg"
            out = self._events_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out), fd.frame,
                        [cv2.IMWRITE_JPEG_QUALITY, int(settings.event_snapshot_quality)])
            # Store the path RELATIVE to the events root — maps 1:1 to the
            # future S3 key (<device_id>/<date>/<event_id>.jpg).
            event.snapshot_path = str(rel).replace("\\", "/")
        except Exception:
            logger.exception("snapshot write failed event=%s", event.event_id)

    # Resolve a stored snapshot_path back to an absolute file (for serving) —
    # delegates to the shared resolver (common.event_snapshots).
    def snapshot_file(self, snapshot_path: str) -> Path | None:
        return event_snapshots.snapshot_file(snapshot_path)
