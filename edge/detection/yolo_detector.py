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
    YOLO TensorRT detector (person-only filter), format-robust.

    Auto-detects the engine's output layout on first inference and parses
    accordingly — so it works whether the engine was built from a YOLO26
    end-to-end export or a raw YOLOv8/26 head:

      - End-to-end (YOLO26 default):  (1, 300, 6) = [x1,y1,x2,y2,conf,cls]
        -> just confidence-filter, no NMS.
      - Raw head:                     (1, 4+nc, 8400) or (1, 8400, 4+nc)
        = [cx,cy,w,h, class_scores...] -> argmax + threshold + NMS.

    Preprocess uses cv2.dnn.blobFromImage — optimized C++ path.
    """

    PERSON_CLASS_ID = 0

    def __init__(self) -> None:
        self._input_size = settings.detection_input_size
        self._confidence_threshold = settings.detection_confidence_threshold
        self._engine = TensorRTEngine(settings.detection_model_path)
        self._layout: str | None = None     # "e2e" | "raw", decided on first call
        logger.info(
            "YOLODetector initialized (input=%d, conf>=%.2f)",
            self._input_size, self._confidence_threshold,
        )

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
        dets = self._postprocess(outputs, camera_id, frame_id, timestamp, w0, h0)
        return DetectionResult(
            camera_id=camera_id, frame_id=frame_id,
            timestamp=timestamp, detections=dets,
        )

    # ---- preprocess --------------------------------------------------
    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1.0 / 255.0,
            size=(self._input_size, self._input_size),
            swapRB=True,
            crop=False,
        )
        return np.ascontiguousarray(blob, dtype=np.float32)

    # ---- postprocess -------------------------------------------------
    def _postprocess(self, outputs, camera_id, frame_id, timestamp,
                     w0, h0) -> list[Detection]:
        if not outputs:
            return []
        preds = np.squeeze(outputs[0])          # drop batch dim

        # Decide layout once, and log the real shape so the format is never
        # a mystery again.
        if self._layout is None:
            self._layout = self._detect_layout(preds)
            logger.info(
                "detector output shape=%s -> layout=%s",
                tuple(np.shape(outputs[0])), self._layout,
            )

        if preds.ndim != 2:
            return []

        if self._layout == "e2e":
            rows = preds                         # (300, 6)
        else:
            # raw: make it (N, 4+nc)
            rows = preds.T if preds.shape[0] < preds.shape[1] else preds

        sx = w0 / self._input_size
        sy = h0 / self._input_size

        if self._layout == "e2e":
            return self._parse_e2e(rows, sx, sy, camera_id, frame_id, timestamp)
        return self._parse_raw(rows, sx, sy, camera_id, frame_id, timestamp)

    @staticmethod
    def _detect_layout(preds: np.ndarray) -> str:
        if preds.ndim != 2:
            return "e2e"
        r, c = preds.shape
        # End-to-end rows are short (6 cols); raw heads carry 4+nc (>=5) on
        # the channel axis and thousands of anchors on the other.
        return "e2e" if min(r, c) == 6 else "raw"

    # ---- end-to-end: [x1,y1,x2,y2,conf,cls] --------------------------
    def _parse_e2e(self, rows, sx, sy, camera_id, frame_id, timestamp):
        if rows.shape[1] < 6:
            return []
        conf = rows[:, 4]
        cls = rows[:, 5].astype(np.int32)
        mask = (conf >= self._confidence_threshold) & (cls == self.PERSON_CLASS_ID)
        kept = rows[mask]
        out: list[Detection] = []
        for p in kept:
            out.append(self._mk(p[0] * sx, p[1] * sy, p[2] * sx, p[3] * sy,
                                float(p[4]), camera_id, frame_id, timestamp))
        return out

    # ---- raw head: [cx,cy,w,h, class scores...] ----------------------
    def _parse_raw(self, rows, sx, sy, camera_id, frame_id, timestamp):
        if rows.shape[1] < 5:
            return []
        person_scores = rows[:, 4 + self.PERSON_CLASS_ID]
        mask = person_scores >= self._confidence_threshold
        cand = rows[mask]
        scores = person_scores[mask]
        if cand.shape[0] == 0:
            return []

        boxes = []
        for p in cand:
            cx, cy, w, h = p[0], p[1], p[2], p[3]
            boxes.append([cx - w / 2, cy - h / 2, w, h])   # xywh for NMS
        idxs = cv2.dnn.NMSBoxes(
            boxes, scores.tolist(),
            self._confidence_threshold, nms_threshold=0.45,
        )
        out: list[Detection] = []
        for i in np.array(idxs).flatten():
            x, y, w, h = boxes[i]
            out.append(self._mk(x * sx, y * sy, (x + w) * sx, (y + h) * sy,
                                float(scores[i]), camera_id, frame_id, timestamp))
        return out

    # ---- helper ------------------------------------------------------
    @staticmethod
    def _mk(x1, y1, x2, y2, conf, camera_id, frame_id, timestamp) -> Detection:
        return Detection(
            camera_id=camera_id, frame_id=frame_id, timestamp=timestamp,
            class_id=YOLODetector.PERSON_CLASS_ID,
            class_name=DetectionClass.PERSON, confidence=conf,
            bbox=BoundingBox(x1=float(x1), y1=float(y1),
                             x2=float(x2), y2=float(y2)),
        )
