from __future__ import annotations

import logging

import cv2
import numpy as np

from common.letterbox import letterbox, to_blob
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
    YOLO TensorRT detector (person-only), format- and layout-robust.

    - Letterbox preprocessing (aspect-preserving) so detection scores don't
      collapse on non-square camera frames.
    - Auto-detects the engine output layout on first inference:
        end-to-end (YOLO26 default): (1, 300, 6) -> just confidence-filter
        raw head:                    (1, 4+nc, 8400) -> argmax + NMS
      and which of the last two columns is the score vs the class id.
    - Logs the real shape + a sample row once, so the format is never a guess.
    """

    PERSON_CLASS_ID = 0

    def __init__(self) -> None:
        self._size = settings.detection_input_size
        self._thr = settings.detection_confidence_threshold
        self._engine = TensorRTEngine(settings.detection_model_path)
        self._layout: str | None = None
        self._score_col: int | None = None
        self._logged = False
        logger.info("YOLODetector initialized (input=%d, conf>=%.2f)",
                    self._size, self._thr)

    # ---- public ------------------------------------------------------
    def detect(self, frame, camera_id, frame_id, timestamp) -> DetectionResult:
        canvas, r, dw, dh = letterbox(frame, self._size)
        outputs = self._engine.infer(to_blob(canvas))
        dets = self._postprocess(outputs, camera_id, frame_id, timestamp, r, dw, dh)
        return DetectionResult(camera_id=camera_id, frame_id=frame_id,
                               timestamp=timestamp, detections=dets)

    # ---- postprocess -------------------------------------------------
    def _postprocess(self, outputs, camera_id, frame_id, timestamp,
                     r, dw, dh) -> list[Detection]:
        if not outputs:
            return []
        preds = np.squeeze(outputs[0])
        if preds.ndim != 2:
            return []

        if self._layout is None:
            self._layout = "e2e" if min(preds.shape) == 6 else "raw"
            logger.info("detector output shape=%s -> layout=%s",
                        tuple(np.shape(outputs[0])), self._layout)

        if self._layout == "e2e":
            return self._parse_e2e(preds, r, dw, dh,
                                   camera_id, frame_id, timestamp)
        rows = preds.T if preds.shape[0] < preds.shape[1] else preds
        return self._parse_raw(rows, r, dw, dh, camera_id, frame_id, timestamp)

    def _unlb(self, x, y, r, dw, dh):
        return (x - dw) / r, (y - dh) / r

    # ---- end-to-end: [x1,y1,x2,y2, {score,class}] --------------------
    def _parse_e2e(self, rows, r, dw, dh, camera_id, frame_id, timestamp):
        if rows.shape[1] < 6:
            return []
        # Decide once which column is the score: the class column holds
        # integer ids (a person is class 0); the score column is in [0,1].
        if self._score_col is None:
            c4max, c5max = float(rows[:, 4].max()), float(rows[:, 5].max())
            if c4max <= 1.0001 and c5max > 1.0001:
                self._score_col = 4
            elif c5max <= 1.0001 and c4max > 1.0001:
                self._score_col = 5
            else:                       # person-only / empty -> score varies, class=0
                self._score_col = 4 if c4max >= c5max else 5
            logger.info("detector score column = %d (class = %d)",
                        self._score_col, 9 - self._score_col)
        sc, cc = self._score_col, 9 - self._score_col

        conf = rows[:, sc]
        cls = rows[:, cc].astype(np.int32)
        mask = (conf >= self._thr) & (cls == self.PERSON_CLASS_ID)

        if not self._logged:
            self._logged = True
            logger.info("detector top row=%s maxscore=%.3f kept=%d",
                        np.round(rows[0], 3).tolist(), float(conf.max()),
                        int(mask.sum()))

        out = []
        for p in rows[mask]:
            x1, y1 = self._unlb(p[0], p[1], r, dw, dh)
            x2, y2 = self._unlb(p[2], p[3], r, dw, dh)
            out.append(self._mk(x1, y1, x2, y2, float(p[sc]),
                                camera_id, frame_id, timestamp))
        return out

    # ---- raw head: [cx,cy,w,h, class scores...] ----------------------
    def _parse_raw(self, rows, r, dw, dh, camera_id, frame_id, timestamp):
        if rows.shape[1] < 5:
            return []
        scores = rows[:, 4 + self.PERSON_CLASS_ID]
        keep = scores >= self._thr
        cand, sc = rows[keep], scores[keep]
        if cand.shape[0] == 0:
            return []
        boxes = [[p[0] - p[2] / 2, p[1] - p[3] / 2, p[2], p[3]] for p in cand]
        idxs = cv2.dnn.NMSBoxes(boxes, sc.tolist(), self._thr, 0.45)
        out = []
        for i in np.array(idxs).flatten():
            x, y, w, h = boxes[i]
            x1, y1 = self._unlb(x, y, r, dw, dh)
            x2, y2 = self._unlb(x + w, y + h, r, dw, dh)
            out.append(self._mk(x1, y1, x2, y2, float(sc[i]),
                                camera_id, frame_id, timestamp))
        return out

    @staticmethod
    def _mk(x1, y1, x2, y2, conf, camera_id, frame_id, timestamp) -> Detection:
        return Detection(
            camera_id=camera_id, frame_id=frame_id, timestamp=timestamp,
            class_id=YOLODetector.PERSON_CLASS_ID,
            class_name=DetectionClass.PERSON, confidence=conf,
            bbox=BoundingBox(x1=float(x1), y1=float(y1),
                             x2=float(x2), y2=float(y2)),
        )
