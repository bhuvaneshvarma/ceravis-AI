from __future__ import annotations

from configuration.config_store import ConfigStore
from schemas.zone import Zone


class ZoneConfig:
    FILE = "zones.json"

    def __init__(self) -> None:
        self.store = ConfigStore()

    def get_all(self) -> list[dict]:
        return self.store.load(self.FILE)

    def get_for_camera(self, camera_id: str) -> list[dict]:
        return [z for z in self.get_all() if z.get("camera_id") == camera_id]

    def add(self, zone: Zone) -> None:
        zones = self.get_all()
        zones.append(zone.model_dump())
        self.store.save(self.FILE, zones)

    def delete(self, zone_id: str) -> bool:
        zones = self.get_all()
        new = [z for z in zones if z.get("zone_id") != zone_id]
        if len(new) == len(zones):
            return False
        self.store.save(self.FILE, new)
        return True

    def replace_for_camera(self, camera_id: str, zones: list[Zone]) -> None:
        others = [z for z in self.get_all() if z.get("camera_id") != camera_id]
        others.extend(z.model_dump() for z in zones)
        self.store.save(self.FILE, others)
