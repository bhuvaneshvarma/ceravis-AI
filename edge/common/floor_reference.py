from __future__ import annotations

"""
Floor reference for scene-aware fall detection.

Reuses the existing Zones tool: draw a polygon over the room floor and give its
name the floor keyword (default "floor", e.g. "living room floor"). That polygon
is the ground plane in image space.

The key test is point-in-polygon on the HEAD point:
  * a STANDING person's head is high in the frame -> OUTSIDE the floor polygon,
  * a FALLEN person's head has dropped to the ground -> INSIDE the floor polygon.
This is naturally view-tolerant (works on an angled, ceiling-ish camera) and
needs no homography or camera calibration — just the one polygon the installer
already knows how to draw.

near_floor() returns:
  True  -> head is on/near the floor (inside a floor polygon)
  False -> head is clearly above the floor
  None  -> no floor polygon drawn for this camera (caller falls back to the
           motion + immobility cues only)
"""

import logging
import time

import cv2
import numpy as np

from config.settings import settings
from configuration.zone_config import ZoneConfig


logger = logging.getLogger("rules")

_CACHE_TTL_SECS = 5.0       # floor polygons rarely change; re-read at most this often


class FloorReference:
    def __init__(self, zone_config: ZoneConfig | None = None) -> None:
        self._zones = zone_config or ZoneConfig()
        self._kw = settings.floor_zone_keyword.strip().lower()
        self._cache: dict[str, list[np.ndarray]] = {}
        self._cache_at: dict[str, float] = {}

    def _floor_polys(self, camera_id: str) -> list[np.ndarray]:
        # Cached — near_floor() is hit at pose FPS, so we must not read the
        # zones file on every call.
        now = time.monotonic()
        if now - self._cache_at.get(camera_id, 0.0) < _CACHE_TTL_SECS:
            return self._cache.get(camera_id, [])
        try:
            zones = self._zones.get_for_camera(camera_id)
        except Exception:
            logger.exception("floor zone lookup failed camera=%s", camera_id)
            return self._cache.get(camera_id, [])
        polys = []
        for z in zones:
            name = str(z.get("zone_name", "")).lower()
            poly = z.get("polygon") or []
            if self._kw in name and len(poly) >= 3:
                polys.append(np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2))
        self._cache[camera_id] = polys
        self._cache_at[camera_id] = now
        return polys

    def near_floor(self, camera_id: str, x: float, y: float) -> bool | None:
        polys = self._floor_polys(camera_id)
        if not polys:
            return None
        for contour in polys:
            if cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0:
                return True
        return False
