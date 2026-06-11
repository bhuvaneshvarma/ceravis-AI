from __future__ import annotations

from fastapi import APIRouter, HTTPException

from configuration.zone_config import ZoneConfig
from schemas.zone import Zone


router = APIRouter(prefix="/api/v1/zones", tags=["Zones"])
zone_config = ZoneConfig()


@router.get("")
def list_zones(camera_id: str | None = None):
    if camera_id:
        return zone_config.get_for_camera(camera_id)
    return zone_config.get_all()


@router.post("")
def create_zone(zone: Zone):
    zone_config.add(zone)
    return {"status": "created", "zone_id": zone.zone_id}


@router.put("/camera/{camera_id}")
def replace_zones(camera_id: str, zones: list[Zone]):
    """Replace ALL zones for a camera in one shot (used by the UI)."""
    for z in zones:
        if z.camera_id != camera_id:
            raise HTTPException(400, "zone.camera_id must match path camera_id")
    zone_config.replace_for_camera(camera_id, zones)
    return {"status": "ok", "count": len(zones)}


@router.delete("/{zone_id}")
def delete_zone(zone_id: str):
    if not zone_config.delete(zone_id):
        raise HTTPException(404, "zone not found")
    return {"status": "deleted", "zone_id": zone_id}
