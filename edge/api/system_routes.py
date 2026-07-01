from __future__ import annotations

"""
System endpoints: root redirect, health, and the live streams (WebSocket JPEG,
HTTP MJPEG, and the alert push socket). Kept here so main.py is just app wiring.
"""

import re

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse

from config.settings import settings
from streaming.websocket_stream import mjpeg_camera, stream_camera


router = APIRouter()


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _resolve_camera(label: str) -> str:
    """A stream label may be the camera id, its display name, or its room
    (slugified) — resolve any of them to the actual camera id."""
    try:
        from configuration.camera_config import CameraConfig
        cams = CameraConfig().get_all()
    except Exception:
        return label
    for c in cams:                                   # exact id wins
        if c.camera_id == label:
            return c.camera_id
    sl = _slugify(label)
    for c in cams:
        if sl and sl in (_slugify(c.camera_name), _slugify(c.room_name),
                         _slugify(c.camera_id)):
            return c.camera_id
    return label


@router.get("/")
def root():
    return RedirectResponse("/ui/live.html")


@router.get("/health")
async def health():
    return {"status": "ok", "version": settings.app_version,
            "device_id": settings.device_id}


@router.websocket("/stream/{camera_id}")
async def websocket_stream(websocket: WebSocket, camera_id: str):
    await stream_camera(websocket,
                        websocket.app.state.camera_manager.frame_buffer, camera_id)


@router.get("/stream.mjpeg/{label}")
async def mjpeg_stream(label: str, request: Request):
    """HTTP(S) MJPEG live stream. `label` may be the camera id, its display name,
    or its room (slugified) — all resolve to the same camera. Embeddable in an
    <img>/browser and the link handed to the cloud monitor."""
    frame_buffer = request.app.state.camera_manager.frame_buffer
    return StreamingResponse(
        mjpeg_camera(frame_buffer, _resolve_camera(label)),
        media_type="multipart/x-mixed-replace; boundary=frame")


@router.websocket("/alerts/stream")
async def websocket_alerts(websocket: WebSocket):
    """Real-time push of enriched events/alerts to the monitor console."""
    await websocket.accept()
    bc = getattr(websocket.app.state, "alert_broadcaster", None)
    if bc is None:
        await websocket.close()
        return
    q = await bc.register()
    try:
        while True:
            payload = await q.get()
            await websocket.send_json(payload)
    except Exception:
        pass
    finally:
        await bc.unregister(q)
