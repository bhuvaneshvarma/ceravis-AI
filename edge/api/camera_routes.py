from __future__ import annotations

from dataclasses import asdict

import cv2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from configuration.camera_config import CameraConfig
from ingestion.camera_manager import CameraManager
from schemas.cameras import Camera


router = APIRouter(
    prefix="/api/v1/cameras",
    tags=["Cameras"],
)

camera_config = CameraConfig()


def _mgr(request: Request) -> CameraManager:
    return request.app.state.camera_manager


# =====================================================================
# STATIC PATHS FIRST (required so they don't collide with /{camera_id})
# =====================================================================

@router.get("")
def list_cameras():
    return camera_config.get_all()


@router.post("")
def create_camera(camera: Camera):
    if camera_config.get_by_id(camera.camera_id):
        raise HTTPException(409, f"Camera exists: {camera.camera_id}")
    camera_config.add(camera)
    return {"status": "created", "camera_id": camera.camera_id}


@router.get("/status")
def all_camera_status(request: Request):
    return {
        cid: asdict(s)
        for cid, s in _mgr(request).get_status().items()
    }


# =====================================================================
# PATH-PARAM ROUTES AFTER
# =====================================================================

@router.get("/{camera_id}")
def get_camera(camera_id: str):
    camera = camera_config.get_by_id(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    return camera


@router.put("/{camera_id}")
def update_camera(camera_id: str, camera: Camera):
    if not camera_config.update(camera_id, camera):
        raise HTTPException(404, "Camera not found")
    return {"status": "updated", "camera_id": camera_id}


@router.delete("/{camera_id}")
def delete_camera(camera_id: str):
    if not camera_config.delete(camera_id):
        raise HTTPException(404, "Camera not found")
    return {"status": "deleted", "camera_id": camera_id}


@router.get("/{camera_id}/status")
def camera_status(camera_id: str, request: Request):
    status = _mgr(request).get_camera_status(camera_id)
    if status is None:
        raise HTTPException(404, "Camera not found")
    return asdict(status)


@router.post("/{camera_id}/start")
def start_camera(camera_id: str, request: Request):
    if not _mgr(request).start_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    return {"status": "started", "camera_id": camera_id}


@router.post("/{camera_id}/stop")
def stop_camera(camera_id: str, request: Request):
    if not _mgr(request).stop_camera(camera_id):
        raise HTTPException(404, "Camera not running")
    return {"status": "stopped", "camera_id": camera_id}


@router.post("/{camera_id}/restart")
def restart_camera(camera_id: str, request: Request):
    if not _mgr(request).restart_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    return {"status": "restarted", "camera_id": camera_id}


@router.get("/{camera_id}/frame")
def latest_frame_metadata(camera_id: str, request: Request):
    frame = _mgr(request).get_frame(camera_id)
    if frame is None:
        raise HTTPException(404, "No frame available")
    return {
        "camera_id": frame.camera_id,
        "frame_id": frame.frame_id,
        "timestamp": frame.timestamp,
        "width": frame.width,
        "height": frame.height,
        "fps": frame.fps,
    }


@router.get("/{camera_id}/snapshot")
def latest_snapshot(camera_id: str, request: Request, quality: int = 80):
    """JPEG snapshot of the most recent frame — used by the zone labeler."""
    frame = _mgr(request).get_frame(camera_id)
    if frame is None:
        raise HTTPException(404, "No frame available")
    ok, buf = cv2.imencode(
        ".jpg", frame.frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)],
    )
    if not ok:
        raise HTTPException(500, "Encode failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@router.post("/probe")
def probe_rtsp(body: dict):
    """
    Lightweight check: try to open an RTSP URL and grab one frame.
    Used by the 'Add camera' UI to confirm a URL works before saving.
    """
    url = (body or {}).get("rtsp_url", "").strip()
    if not url:
        raise HTTPException(400, "rtsp_url required")
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return {"ok": False, "reason": "could not open stream"}
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return {"ok": False, "reason": "stream opened but no frame received"}
    h, w = frame.shape[:2]
    return {"ok": True, "width": int(w), "height": int(h)}
