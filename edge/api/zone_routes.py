from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from configuration.zone_config import ZoneConfig
from schemas.zone import Zone


router = APIRouter(prefix="/api/v1/zones", tags=["Zones"])
zone_config = ZoneConfig()
logger = logging.getLogger("zones")


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
    """Replace ALL zones for a camera in one shot (used by the UI). Zones are
    (re)id'd deterministically per camera — '<camera_id>_zone_<n>' — so ids are
    stable and unambiguous instead of random. This ONLY persists locally; the
    single consolidated cloud upload happens once, on /finalize (wizard Continue)
    — so per-camera saves never spawn multiple cloud files."""
    out: list[Zone] = []
    for i, z in enumerate(zones, 1):
        if z.camera_id != camera_id:
            raise HTTPException(400, "zone.camera_id must match path camera_id")
        z.zone_id = f"{camera_id}_zone_{i}"
        out.append(z)
    zone_config.replace_for_camera(camera_id, out)
    return {"status": "ok", "count": len(out)}


@router.delete("/{zone_id}")
def delete_zone(zone_id: str):
    if not zone_config.delete(zone_id):
        raise HTTPException(404, "zone not found")
    return {"status": "deleted", "zone_id": zone_id}


def _zones_by_camera() -> dict:
    """All saved zones grouped by camera_id — {'<camera_id>': [zone, ...], …}:
    the ONE consolidated shape uploaded as zones_<userId>.json (a dictionary per
    camera), so the cloud holds exactly one zones file per patient."""
    grouped: dict[str, list] = {}
    for z in zone_config.get_all():
        grouped.setdefault(z.get("camera_id", ""), []).append(z)
    return grouped


@router.post("/finalize")
def finalize_zones():
    """Upload the ONE consolidated zones file (all cameras, grouped per camera)
    to the app server as zones_<ceravisUserId>.json — fileCategory ZONING. Called
    exactly once, when the wizard leaves the zoning step (Continue), so per-camera
    saves stay local and only a single zones file is ever written to the cloud."""
    from configuration.account_config import patient_user_id
    from integration.ceravis_api import (CeravisApiError, is_configured,
                                          upload_embedding_file)
    grouped = _zones_by_camera()
    summary = {"cameras": len(grouped),
               "zones": sum(len(v) for v in grouped.values())}
    pid = patient_user_id()
    if not is_configured() or not pid:
        return {"uploaded": False,
                "reason": "app server not configured / account not verified",
                **summary}
    data = json.dumps(grouped, indent=2).encode("utf-8")
    try:
        upload_embedding_file("ZONING", pid, f"zones_{pid}.json", data)
    except CeravisApiError as exc:
        logger.warning("zones finalize upload failed: %s", exc)
        return {"uploaded": False, "reason": str(exc), **summary}
    return {"uploaded": True, "file": f"zones_{pid}.json", **summary}
