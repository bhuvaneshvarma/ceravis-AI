from __future__ import annotations

import logging

from configuration.camera_config import CameraConfig

from ingestion.camera_status import (
    CameraHealthState,
    CameraStatus,
)

from ingestion.frame_buffer import FrameBuffer
from ingestion.frame_data import FrameData

from ingestion.rtsp_reader import RTSPReader
from ingestion.stream_registry import StreamRegistry


logger = logging.getLogger("ingestion")


class CameraManager:
    """
    Central orchestration service for all cameras.

    Responsibilities:
    - Start all configured cameras
    - Stop all cameras
    - Restart individual cameras
    - Expose camera status
    - Expose latest frames

    This is the single entrypoint into the ingestion subsystem.

    `via_mediamtx=True` makes every reader pull the MediaMTX localhost
    restream instead of the camera directly (the camera then has exactly one
    consumer: MediaMTX).
    """

    def __init__(self, via_mediamtx: bool = False) -> None:

        self._camera_config = CameraConfig()

        self._frame_buffer = FrameBuffer()

        self._stream_registry = StreamRegistry()

        self._via_mediamtx = via_mediamtx

    # =====================================================
    # Properties
    # =====================================================

    @property
    def frame_buffer(self) -> FrameBuffer:
        return self._frame_buffer

    @property
    def stream_registry(self) -> StreamRegistry:
        return self._stream_registry

    # =====================================================
    # Lifecycle
    # =====================================================

    def start_all(self) -> None:
        """
        Start all enabled cameras.
        """

        cameras = self._camera_config.get_enabled()

        started_count = 0

        for camera in cameras:

            if self.start_camera(camera.camera_id):
                started_count += 1

        logger.info(
            "Started %s/%s cameras",
            started_count,
            len(cameras),
        )

    def stop_all(self) -> None:
        """
        Gracefully stop all cameras.
        """

        readers = self._stream_registry.get_all()

        for reader in readers.values():
            reader.stop()

        for reader in readers.values():
            reader.join(timeout=10)

        self._stream_registry = StreamRegistry()

        logger.info(
            "All camera streams stopped"
        )

    # =====================================================
    # Individual Camera Control
    # =====================================================

    def start_camera(
        self,
        camera_id: str,
    ) -> bool:
        """
        Start a single camera.
        """

        existing_reader = (
            self._stream_registry.get(camera_id)
        )

        if existing_reader is not None:

            logger.warning(
                "Camera already running: %s",
                camera_id,
            )

            return True

        camera = (
            self._camera_config.get_by_id(
                camera_id
            )
        )

        if camera is None:

            logger.warning(
                "Camera not found: %s",
                camera_id,
            )

            return False

        source_url = None
        if self._via_mediamtx:
            from media.mediamtx_client import local_rtsp_url
            source_url = local_rtsp_url(camera.camera_id)

        try:
            reader = RTSPReader(camera=camera, frame_buffer=self._frame_buffer,
                                source_url=source_url)
            reader.start()
            self._stream_registry.register(camera_id, reader,)
            logger.info("Camera started: %s", camera_id,)
            return True
        except Exception:
            logger.exception("Failed to start camera: %s", camera_id)
            return False

    def stop_camera(
        self,
        camera_id: str,
    ) -> bool:
        """
        Stop a single camera.
        """

        reader = (
            self._stream_registry.get(
                camera_id
            )
        )

        if reader is None:
            return False

        reader.stop()

        reader.join(timeout=10)

        self._stream_registry.unregister(
            camera_id
        )

        self._frame_buffer.clear(
            camera_id
        )

        logger.info(
            "Camera stopped: %s",
            camera_id,
        )

        return True

    def restart_camera(
        self,
        camera_id: str,
    ) -> bool:
        """
        Restart a camera.
        """

        self.stop_camera(camera_id)

        return self.start_camera(
            camera_id
        )

    # =====================================================
    # Status
    # =====================================================

    def get_status(
        self,
    ) -> dict[str, CameraStatus]:
        """
        Return status of all configured cameras.
        """

        status: dict[str, CameraStatus] = {}

        cameras = (
            self._camera_config.get_all()
        )

        for camera in cameras:

            reader = (
                self._stream_registry.get(
                    camera.camera_id
                )
            )

            if reader is None:

                status[
                    camera.camera_id
                ] = CameraStatus(
                    camera_id=camera.camera_id,
                    camera_name=camera.camera_name,
                    room_name=camera.room_name,
                    is_running=False,
                    health_state=CameraHealthState.OFFLINE,
                    frames_captured=0,
                    reconnect_count=0,
                    last_frame_time=None,
                )

                continue

            status[
                camera.camera_id
            ] = CameraStatus(
                camera_id=camera.camera_id,
                camera_name=camera.camera_name,
                room_name=camera.room_name,
                is_running=reader.is_running,
                health_state=reader.health_state,
                frames_captured=reader.frames_captured,
                reconnect_count=reader.reconnect_count,
                last_frame_time=reader.last_frame_time,
                current_fps=reader.current_fps,
            )

        return status
    
    def get_camera_status(
        self,
        camera_id: str
    ):

        status = self.get_status()

        return status.get(camera_id)

    # =====================================================
    # Frame Access
    # =====================================================

    def get_frame(
        self,
        camera_id: str,
    ) -> FrameData | None:
        """
        Get latest frame for a camera.
        """

        return self._frame_buffer.get(
            camera_id
        )

    def get_all_frames(
        self,
    ) -> dict[str, FrameData]:
        """
        Get latest frames for all cameras.
        """

        return self._frame_buffer.get_all_latest()