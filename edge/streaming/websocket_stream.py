from __future__ import annotations

import asyncio
import logging

import cv2

from fastapi import WebSocket, WebSocketDisconnect

from config.settings import settings
from ingestion.frame_buffer import FrameBuffer


logger = logging.getLogger("streaming")


async def stream_camera(
    websocket: WebSocket,
    frame_buffer: FrameBuffer,
    camera_id: str,
    jpeg_quality: int = 70,
) -> None:
    """
    JPEG-over-WebSocket live stream for a single camera.

    Cheap on the Jetson: only encodes when there's a new frame_id.
    """
    await websocket.accept()
    interval = 1.0 / max(settings.stream_fps, 1.0)
    last_frame_id = -1

    try:
        while True:
            frame_data = frame_buffer.get(camera_id)
            if frame_data is None or frame_data.frame_id == last_frame_id:
                await asyncio.sleep(interval)
                continue

            last_frame_id = frame_data.frame_id
            ok, buf = cv2.imencode(
                ".jpg",
                frame_data.frame,
                [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality],
            )
            if not ok:
                await asyncio.sleep(interval)
                continue

            await websocket.send_bytes(buf.tobytes())
            await asyncio.sleep(interval)

    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Stream error camera=%s", camera_id)
        try:
            await websocket.close()
        except Exception:
            pass
