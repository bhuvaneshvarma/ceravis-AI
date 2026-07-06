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
    from media.mediamtx_client import is_up
    return {"status": "ok", "version": settings.app_version,
            "device_id": settings.device_id,
            # False = live links/recording are dead even though the app is up
            # (mediamtx missing or crashed — see data/mediamtx.log).
            "media_backbone": is_up()}


@router.get("/api/v1/cloud/activity")
def cloud_activity(limit: int = 100):
    """Rolling log of app-server calls (saveAlert/saveSnapshot/…) — feeds the
    monitor's Cloud Sync Console so end-to-end tests are verifiable on screen."""
    from integration.call_log import recent
    return {"calls": recent(limit=min(int(limit), 300))}


@router.websocket("/stream/{camera_id}")
async def websocket_stream(websocket: WebSocket, camera_id: str):
    await stream_camera(websocket,
                        websocket.app.state.camera_manager.frame_buffer, camera_id)
