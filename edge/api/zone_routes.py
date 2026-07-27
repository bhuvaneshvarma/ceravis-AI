from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException

from configuration.zone_config import ZoneConfig
from schemas.zone import Zone


router = APIRouter(prefix="/api/v1/zones", tags=["Zones"])
zone_config = ZoneConfig()
logger = logging.getLogger("zones")


def _upload_zones() -> None:
    """Best-effort: mirror the WHOLE zones.json to the app server
    (uploadEmbeddingFile, fileCategory ZONING) so the room layout follows the
    patient's account and survives a device reflash. Scheduled as a background
    task so it never delays the zone save; a transport error is logged only."""
    try:
        from configuration.account_config import patient_user_id
        from integration.ceravis_api import is_configured, upload_embedding_file
        if not is_configured():
            return
        pid = patient_user_id()               # = ceravisUserId (the account)
        if not pid:
            return
        data = json.dumps(zone_config.get_all(), indent=2).encode("utf-8")
        upload_embedding_file("ZONING", pid, f"zones_{pid}.json", data)
    except Exception:
        logger.warning("zones upload failed", exc_info=True)


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
def replace_zones(camera_id: str, zones: list[Zone],
                  background_tasks: BackgroundTasks):
    """Replace ALL zones for a camera in one shot (used by the UI). After the
    save, the whole zones.json is mirrored to the cloud in the background."""
    for z in zones:
        if z.camera_id != camera_id:
            raise HTTPException(400, "zone.camera_id must match path camera_id")
    zone_config.replace_for_camera(camera_id, zones)
    background_tasks.add_task(_upload_zones)
    return {"status": "ok", "count": len(zones)}


@router.delete("/{zone_id}")
def delete_zone(zone_id: str):
    if not zone_config.delete(zone_id):
        raise HTTPException(404, "zone not found")
    return {"status": "deleted", "zone_id": zone_id}
