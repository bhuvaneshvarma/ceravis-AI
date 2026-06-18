from __future__ import annotations

import logging

import cv2
import numpy as np

from configuration.zone_config import ZoneConfig


logger = logging.getLogger("rules")


class ZoneResolver:
    """
    Resolve which named functional area (zone) a point falls in, per camera.

    Zones are the named polygons drawn during setup (fridge, bed, couch, …).
    Membership is a point-in-polygon test on the person's FOOT point (the
    bottom-centre of the bbox) — that's where the person actually stands, so it
    maps a body to a floor area far better than the bbox centre.

    Zones are read from config on demand; events are infrequent (rule engine
    runs at 1 Hz and only enriches emitted events), so this stays cheap.
    """

    def __init__(self, zone_config: ZoneConfig | None = None) -> None:
        self._zones = zone_config or ZoneConfig()

    def area_for(self, camera_id: str, x: float, y: float) -> str | None:
        """Name of the first zone containing (x, y) on this camera, else None."""
        try:
            zones = self._zones.get_for_camera(camera_id)
        except Exception:
            logger.exception("zone lookup failed camera=%s", camera_id)
            return None
        for z in zones:
            poly = z.get("polygon") or []
            if len(poly) < 3:
                continue
            contour = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            if cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0:
                return z.get("zone_name")
        return None
