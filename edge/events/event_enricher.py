from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import cv2

from common.annotate import annotate_event_frame
from common.zone_resolver import ZoneResolver
from config.settings import settings
from configuration.camera_config import CameraConfig
from rules.rule_context import RuleContext
from schemas.event import Event


logger = logging.getLogger("rules")

# edge/ project root (this file is edge/events/event_enricher.py).
_EDGE_ROOT = Path(__file__).resolve().parents[1]

# event_type -> (severity, human title)
_ALERT_MAP: dict[str, tuple[str, str]] = {
    "fall": ("critical", "Fall detected"),
    "lying_down": ("info", "Lying down"),
    "visitor": ("warning", "Visitor present"),
    "no_movement": ("warning", "No movement"),
    "prolonged_sitting": ("info", "Prolonged sitting"),
    "standing_up": ("info", "Stood up"),
    "walking_started": ("info", "Started walking"),
    "inactivity": ("warning", "Prolonged inactivity"),
    "area_transition": ("info", "Moved area"),
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
        edir = Path(settings.events_dir)
        self._events_root = edir if edir.is_absolute() else (_EDGE_ROOT / edir)
        self._rest_kw = [k.strip().lower()
                         for k in settings.rest_zone_keywords.split(",") if k.strip()]

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
        who = event.recipient_id or "person"
        event.message = f"{title} — {who}" + (f" in {loc}" if loc.strip() else "")
        if event.detail:
            event.message += f" · {event.detail}"

        self._write_snapshot(event, ctx, bbox, area)
        return event

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

    def _write_snapshot(self, event: Event, ctx: RuleContext, bbox, area) -> None:
        try:
            fd = ctx.frames.get(event.camera_id)
            if fd is None:
                return
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rel = Path(settings.device_id) / day / f"{event.event_id}.jpg"
            out = self._events_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            caption = f"{event.title} · {area or event.room_name}".strip(" ·")
            box_label = (event.recipient_id or "person")
            img = annotate_event_frame(fd.frame, bbox, caption, box_label)
            cv2.imwrite(str(out), img,
                        [cv2.IMWRITE_JPEG_QUALITY, int(settings.event_snapshot_quality)])
            # Store the path RELATIVE to the events root — maps 1:1 to the
            # future S3 key (<device_id>/<date>/<event_id>.jpg).
            event.snapshot_path = str(rel).replace("\\", "/")
        except Exception:
            logger.exception("snapshot write failed event=%s", event.event_id)

    # Resolve a stored snapshot_path back to an absolute file (for serving).
    def snapshot_file(self, snapshot_path: str) -> Path | None:
        safe = Path(snapshot_path)
        if safe.is_absolute() or ".." in safe.parts:
            return None
        f = self._events_root / safe
        return f if f.exists() else None
