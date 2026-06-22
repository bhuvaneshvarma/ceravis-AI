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
    jpeg_quality: int | None = None,
) -> None:
    """
    JPEG-over-WebSocket live stream for a single camera.

    Cheap on the Jetson: only encodes when there's a new frame_id, and the
    JPEG encode runs in a worker thread (cv2 releases the GIL) so heavy
    inference on the event loop can't stall the stream. Optionally downscales
    to settings.stream_max_width for a lighter live wall.
    """
    await websocket.accept()
    interval = 1.0 / max(settings.stream_fps, 1.0)
    quality = jpeg_quality if jpeg_quality is not None else settings.stream_jpeg_quality
    max_w = settings.stream_max_width
    last_frame_id = -1

    try:
        while True:
            frame_data = frame_buffer.get(camera_id)
            if frame_data is None or frame_data.frame_id == last_frame_id:
                await asyncio.sleep(interval)
                continue

            last_frame_id = frame_data.frame_id
            frame = frame_data.frame
            if max_w and frame.shape[1] > max_w:        # downscale only (never upscale)
                scale = max_w / frame.shape[1]
                frame = cv2.resize(
                    frame, (max_w, max(1, int(frame.shape[0] * scale))),
                    interpolation=cv2.INTER_AREA,
                )

            ok, buf = await asyncio.to_thread(
                cv2.imencode, ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality],
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


async def mjpeg_camera(
    frame_buffer: FrameBuffer,
    camera_id: str,
    jpeg_quality: int | None = None,
):
    """
    MJPEG (multipart/x-mixed-replace) live stream for a single camera.

    Plain HTTP(S), so it embeds directly in an <img> tag or any browser/app and
    is the link we hand the cloud monitor (https-able via a TLS reverse proxy).
    Same cheap encode-only-on-new-frame, off-the-loop policy as the WS stream.
    """
    interval = 1.0 / max(settings.stream_fps, 1.0)
    quality = jpeg_quality if jpeg_quality is not None else settings.stream_jpeg_quality
    max_w = settings.stream_max_width
    last_frame_id = -1
    try:
        while True:
            frame_data = frame_buffer.get(camera_id)
            if frame_data is None or frame_data.frame_id == last_frame_id:
                await asyncio.sleep(interval)
                continue
            last_frame_id = frame_data.frame_id
            frame = frame_data.frame
            if max_w and frame.shape[1] > max_w:
                scale = max_w / frame.shape[1]
                frame = cv2.resize(
                    frame, (max_w, max(1, int(frame.shape[0] * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, buf = await asyncio.to_thread(
                cv2.imencode, ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality],
            )
            if not ok:
                await asyncio.sleep(interval)
                continue
            jpg = buf.tobytes()
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(interval)
    except (asyncio.CancelledError, GeneratorExit):
        return
    except Exception:
        logger.exception("MJPEG stream error camera=%s", camera_id)
