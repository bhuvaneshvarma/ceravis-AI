from __future__ import annotations

"""
System endpoints: root redirect, health, and the built-in UI's WebSocket JPEG
live stream. External live viewing (cloud, browsers) is served by MediaMTX
(WebRTC/HLS) — not by this process. Kept here so main.py is just app wiring.
"""

from fastapi import APIRouter, WebSocket
from fastapi.responses import RedirectResponse

from config.settings import settings
from streaming.websocket_stream import stream_camera


router = APIRouter()


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
