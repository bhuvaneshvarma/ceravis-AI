from __future__ import annotations

import logging
from datetime import datetime

import cv2
import numpy as np

from config.settings import settings
from detection.trt_engine import TensorRTEngine
from pose.pose_schema import Keypoint, PoseEstimation, PoseResult


logger = logging.getLogger("pose")


class YOLOPose:
    """
    YOLO26-Pose / YOLOv8-Pose TensorRT (17-keypoint COCO).

    Handles both Ultralytics output layouts:
      - YOLO26 end-to-end:  (1, N, 57) -> [x1,y1,x2,y2,conf,cls, 17*(x,y,c)]
      - YOLOv8 raw:         (1, 56, N) -> [x,y,w,h,conf,      17*(x,y,c)]
    The keypoint block is always the trailing 51 values; confidence is
    always column 4 — so we locate keypoints from the column count.
    """

    NUM_KP = 17

    def __init__(self) -> None:
        self._input_size = settings.pose_input_size
        self._conf_thr = settings.pose_confidence_threshold
        self._engine = TensorRTEngine(settings.pose_model_path)
        logger.info("YOLOPose initialized")

    def estimate(
        self,
        frame: np.ndarray,
        camera_id: str,
        frame_id: int,
        timestamp: datetime,
    ) -> PoseResult:
        h0, w0 = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1.0 / 255.0,
            size=(self._input_size, self._input_size),
            swapRB=True,
            crop=False,
        )
        out = self._engine.infer(np.ascontiguousarray(blob, dtype=np.float32))
        if not out:
            return PoseResult(
                camera_id=camera_id, frame_id=frame_id,
                timestamp=timestamp, poses=[],
            )

        preds = out[0]
        if preds.ndim == 3:
            preds = preds[0]

        # Normalize to rows = predictions. Channel-first layouts (56/57, N)
        # get transposed.
        if preds.shape[0] in (56, 57) and preds.shape[1] > preds.shape[0]:
            preds = preds.T
        if preds.shape[-1] < 51 + 5:
            return PoseResult(
                camera_id=camera_id, frame_id=frame_id,
                timestamp=timestamp, poses=[],
            )
        kp_off = preds.shape[-1] - 3 * self.NUM_KP   # 5 (v8) or 6 (26 e2e)

        sx = w0 / self._input_size
        sy = h0 / self._input_size
        poses: list[PoseEstimation] = []
        for p in preds:
            conf = float(p[4])
            if conf < self._conf_thr:
                continue
            kps_flat = p[kp_off:]
            kps: list[Keypoint] = []
            for i in range(self.NUM_KP):
                kx, ky, kc = kps_flat[3 * i: 3 * i + 3]
                kps.append(
                    Keypoint(
                        x=float(kx * sx),
                        y=float(ky * sy),
                        confidence=float(kc),
                    )
                )
            poses.append(
                PoseEstimation(
                    track_id=None,
                    camera_id=camera_id,
                    frame_id=frame_id,
                    timestamp=timestamp,
                    keypoints=kps,
                )
            )
        return PoseResult(
            camera_id=camera_id,
            frame_id=frame_id,
            timestamp=timestamp,
            poses=poses,
        )
