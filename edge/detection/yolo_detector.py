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
    YOLO26 TensorRT detector (person-only filter).

    Expects the end-to-end (NMS-free) export layout: each prediction row is
    [x1, y1, x2, y2, conf, cls] — YOLO26's default ONNX output. Raw YOLOv8
    (84, 8400) outputs are NOT supported; re-export with YOLO26 weights.

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

        # Guard: raw anchor-grid layouts (e.g. v8's 84x8400) mean the engine
        # was built from the wrong export — fail loudly, not silently.
        if preds.ndim != 2 or preds.shape[-1] < 6 or preds.shape[0] > 1000:
            logger.error(
                "Unexpected detector output shape %s — engine must be a "
                "YOLO26 end-to-end export ([N,6] rows). Re-run "
                "scripts/export_engines.sh", preds.shape,
            )
            return []

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
