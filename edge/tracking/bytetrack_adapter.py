from __future__ import annotations

import numpy as np

from detection.detection_schema import (
    Detection,
    DetectionClass,
    DetectionResult,
)


class ByteTrackAdapter:
    """
    Converts Ceravis DetectionResult into the ByteTrack-shaped
    numpy array (N, 5) = [x1, y1, x2, y2, conf]. Person-only.
    """

    @staticmethod
    def to_bytetrack(result: DetectionResult) -> np.ndarray:
        rows = [
            [d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2, d.confidence]
            for d in result.detections
            if d.class_name == DetectionClass.PERSON
        ]
        if not rows:
            return np.empty((0, 5), dtype=np.float32)
        return np.asarray(rows, dtype=np.float32)

    @staticmethod
    def person_count(result: DetectionResult) -> int:
        return sum(
            1 for d in result.detections
            if d.class_name == DetectionClass.PERSON
        )

    @staticmethod
    def extract_persons(result: DetectionResult) -> list[Detection]:
        return [
            d for d in result.detections
            if d.class_name == DetectionClass.PERSON
        ]
