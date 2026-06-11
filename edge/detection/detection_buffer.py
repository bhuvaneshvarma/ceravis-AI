from __future__ import annotations

from threading import RLock

from detection.detection_schema import (
    DetectionResult,
)


class DetectionBuffer:
    """
    Thread-safe storage for latest detections.

    Stores one DetectionResult
    per camera.

    Writer:
        DetectionRunner

    Readers:
        Tracking
        ReID
        Pose
        Rules
        APIs
    """

    def __init__(self) -> None:

        self._lock = RLock()

        self._detections: dict[
            str,
            DetectionResult
        ] = {}

    # ====================================================
    # Write
    # ====================================================

    def update(
        self,
        result: DetectionResult,
    ) -> None:

        with self._lock:

            self._detections[
                result.camera_id
            ] = result

    # ====================================================
    # Read
    # ====================================================

    def get(
        self,
        camera_id: str,
    ) -> DetectionResult | None:

        with self._lock:

            return self._detections.get(
                camera_id
            )

    def get_all(
        self,
    ) -> dict[str, DetectionResult]:

        with self._lock:

            return dict(
                self._detections
            )

    # ====================================================
    # Maintenance
    # ====================================================

    def clear(
        self,
        camera_id: str,
    ) -> None:

        with self._lock:

            self._detections.pop(
                camera_id,
                None,
            )

    def clear_all(
        self,
    ) -> None:

        with self._lock:

            self._detections.clear()