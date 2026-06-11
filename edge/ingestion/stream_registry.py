from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ingestion.rtsp_reader import RTSPReader


class StreamRegistry:
    """
    Registry of active RTSP readers.
    """

    def __init__(self) -> None:

        self._lock = Lock()

        self._readers: dict[str, RTSPReader] = {}


    def register(
        self,
        camera_id: str,
        reader: "RTSPReader"
    ) -> None:

        with self._lock:
            self._readers[camera_id] = reader


    def unregister(
        self,
        camera_id: str
    ) -> None:

        with self._lock:
            self._readers.pop(
                camera_id,
                None
            )


    def get(
        self,
        camera_id: str
    ) -> "RTSPReader | None":

        with self._lock:
            return self._readers.get(camera_id)


    def get_all(
        self
    ) -> dict[str, "RTSPReader"]:

        with self._lock:
            return dict(self._readers)


    def get_active_ids(
        self
    ) -> list[str]:

        with self._lock:
            return [
                camera_id
                for camera_id, reader
                in self._readers.items()
                if reader.is_running
            ]