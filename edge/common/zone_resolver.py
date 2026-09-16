from __future__ import annotations

import logging
import time

import cv2
import numpy as np

from config.settings import settings
from configuration.zone_config import ZoneConfig


logger = logging.getLogger("rules")


class ZoneResolver:
    """
    Resolve which named functional area (zone) a point falls in, per camera.

    Zones are the named polygons drawn during setup (fridge, bed, couch, …).
    Membership is a point-in-polygon test on the person's FOOT point (the
    bottom-centre of the bbox) — that's where the person actually stands, so it
    maps a body to a floor area far better than the bbox centre.

    Zone polygons are cached per camera for a few seconds: the rule enricher
    reads them at 1 Hz and the recording trigger at 2 Hz, and re-reading
    zones.json on every one of those was needless disk churn. A drawn zone still
    takes effect within the TTL, which is imperceptible against setup.
    """

    _CACHE_TTL_SECS = 5.0

    def __init__(self, zone_config: ZoneConfig | None = None) -> None:
        self._zones = zone_config or ZoneConfig()
        self._cache: dict[str, tuple[float, list]] = {}   # cam -> (loaded_at, zones)

    def _zones_for(self, camera_id: str) -> list:
        now = time.monotonic()
        hit = self._cache.get(camera_id)
        if hit is not None and (now - hit[0]) < self._CACHE_TTL_SECS:
            return hit[1]
        try:
            zones = self._zones.get_for_camera(camera_id)
        except Exception:
            logger.exception("zone lookup failed camera=%s", camera_id)
            zones = hit[1] if hit is not None else []      # serve stale over nothing
        self._cache[camera_id] = (now, zones)
        return zones

    @staticmethod
    def _contains(zone: dict, x: float, y: float) -> bool:
        poly = zone.get("polygon") or []
        if len(poly) < 3:
            return False
        contour = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
        return cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0

    def area_for(self, camera_id: str, x: float, y: float) -> str | None:
        """Name of the first zone containing (x, y) on this camera, else None."""
        for z in self._zones_for(camera_id):
            if self._contains(z, x, y):
                return z.get("zone_name")
        return None

    def is_excluded(self, camera_id: str, x: float, y: float) -> bool:
        """True if (x, y) falls in an IGNORE zone — a drawn region the analytics
        must never treat as a person (a TV, a monitor wall, a framed photo, a
        mirror, a chair that will not stop false-firing). Matched by keyword in
        the zone's name (settings.ignore_zone_keywords), reusing the ordinary
        zone-drawing UI with no new schema."""
        kws = [k.strip().lower() for k in settings.ignore_zone_keywords.split(",")
               if k.strip()]
        if not kws:
            return False
        for z in self._zones_for(camera_id):
            name = (z.get("zone_name") or "").lower()
            if any(k in name for k in kws) and self._contains(z, x, y):
                return True
        return False
