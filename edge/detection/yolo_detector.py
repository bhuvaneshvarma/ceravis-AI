from __future__ import annotations

import logging
from datetime import datetime

import cv2
import numpy as np

from config.settings import settings
from detection.detection_schema import (
    BoundingBox,
    Detection,
    DetectionClass,
    DetectionResult,
)
from detection.trt_engine import TensorRTEngine


logger = logging.getLogger("detection")


class YOLODetector:
    """
    YOLOv8n / YOLO26n TensorRT detector (person-only filter).

    Preprocess uses cv2.dnn.blobFromImage — optimized C++ path,
    ~3x faster than manual resize+transpose on Jetson.
    """

    PERSON_CLASS_ID = 0

    def __init__(self) -> None:
        self._input_size = settings.detection_input_size
        self._confidence_threshold = settings.detection_confidence_threshold

        self._engine = TensorRTEngine(settings.detection_model_path)
        logger.info("YOLODetector initialized (input=%d)", self._input_size)

    # ---- public ------------------------------------------------------
    def detect(
        self,
        frame: np.ndarray,
        camera_id: str,
        frame_id: int,
        timestamp: datetime,
    ) -> DetectionResult:
        h0, w0 = frame.shape[:2]
        blob = self._preprocess(frame)
        outputs = self._engine.infer(blob)
        dets = self._postprocess(
            outputs=outputs,
            camera_id=camera_id,
            frame_id=frame_id,
            timestamp=timestamp,
            original_width=w0,
            original_height=h0,
        )
        return DetectionResult(
            camera_id=camera_id,
            frame_id=frame_id,
            timestamp=timestamp,
            detections=dets,
        )

    # ---- preprocess --------------------------------------------------
    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        # blobFromImage handles resize + BGR->RGB + scale + HWC->NCHW in C++.
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1.0 / 255.0,
            size=(self._input_size, self._input_size),
            swapRB=True,
            crop=False,
        )
        return np.ascontiguousarray(blob, dtype=np.float32)

    # ---- postprocess -------------------------------------------------
    def _postprocess(
        self,
        outputs: list[np.ndarray],
        camera_id: str,
        frame_id: int,
        timestamp: datetime,
        original_width: int,
        original_height: int,
    ) -> list[Detection]:
        if not outputs:
            return []

        preds = outputs[0]
        if preds.ndim == 3:
            preds = preds[0]

        sx = original_width / self._input_size
        sy = original_height / self._input_size
        thr = self._confidence_threshold

        # Vectorized filter — much faster than Python loop on Jetson
        conf = preds[:, 4]
        cls = preds[:, 5].astype(np.int32)
        mask = (conf >= thr) & (cls == self.PERSON_CLASS_ID)
        if not mask.any():
            return []

        kept = preds[mask]
        out: list[Detection] = []
        for p in kept:
            out.append(
                Detection(
                    camera_id=camera_id,
                    frame_id=frame_id,
                    timestamp=timestamp,
                    class_id=int(p[5]),
                    class_name=DetectionClass.PERSON,
                    confidence=float(p[4]),
                    bbox=BoundingBox(
                        x1=float(p[0] * sx),
                        y1=float(p[1] * sy),
                        x2=float(p[2] * sx),
                        y2=float(p[3] * sy),
                    ),
                )
            )
        return out
