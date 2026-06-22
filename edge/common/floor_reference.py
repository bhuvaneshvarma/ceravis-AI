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
        self._furn_kw = [k.strip().lower()
                         for k in settings.furniture_zone_keywords.split(",") if k.strip()]
        # one small TTL cache of (floor_polys, furniture_polys) per camera
        self._cache: dict[str, tuple[list, list]] = {}
        self._cache_at: dict[str, float] = {}

    def _polys(self, camera_id: str) -> tuple[list, list]:
        """(floor_polys, furniture_polys) for a camera — cached, since this is
        hit at pose FPS and must not read the zones file every call."""
        now = time.monotonic()
        if now - self._cache_at.get(camera_id, 0.0) < _CACHE_TTL_SECS:
            return self._cache.get(camera_id, ([], []))
        floor, furn = [], []
        try:
            zones = self._zones.get_for_camera(camera_id)
        except Exception:
            logger.exception("zone lookup failed camera=%s", camera_id)
            return self._cache.get(camera_id, ([], []))
        for z in zones:
            name = str(z.get("zone_name", "")).lower()
            poly = z.get("polygon") or []
            if len(poly) < 3:
                continue
            arr = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            if self._kw in name:
                floor.append(arr)
            if any(k in name for k in self._furn_kw):
                furn.append(arr)
        self._cache[camera_id] = (floor, furn)
        self._cache_at[camera_id] = now
        return floor, furn

    def near_floor(self, camera_id: str, x: float, y: float) -> bool | None:
        floor, _ = self._polys(camera_id)
        if not floor:
            return None
        for contour in floor:
            if cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0:
                return True
        return False

    def below_furniture(self, camera_id: str, x: float, y: float) -> bool | None:
        """True if the point is LOWER in the frame than the top of furniture above
        it — i.e. the body has dropped below table/chair/bed height. None if no
        furniture zones are drawn for this camera."""
        _, furn = self._polys(camera_id)
        if not furn:
            return None
        tops = []
        for arr in furn:
            xs, ys = arr[:, 0, 0], arr[:, 0, 1]
            if xs.min() <= x <= xs.max():        # furniture spans this column
                tops.append(int(ys.min()))       # its top edge (smallest y)
        if not tops:
            return False
        return y > min(tops)                     # point is below the highest top

    def is_low(self, camera_id: str, x: float, y: float) -> bool | None:
        """Combined 'the body is on/near the ground OR below furniture height'.
        True if either holds; None if neither reference is drawn."""
        nf = self.near_floor(camera_id, x, y)
        bf = self.below_furniture(camera_id, x, y)
        if nf is True or bf is True:
            return True
        if nf is None and bf is None:
            return None
        return False
