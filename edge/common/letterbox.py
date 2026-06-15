from __future__ import annotations

import cv2
import numpy as np


def letterbox(frame: np.ndarray, size: int = 640,
              color: int = 114) -> tuple[np.ndarray, float, int, int]:
    """
    Aspect-preserving resize + pad to a square (the preprocessing YOLO models
    are trained/exported with). Squishing a 16:9 frame straight to 640x640
    distorts people enough that detection scores collapse — hence letterbox.

    Returns (canvas, ratio, pad_x, pad_y). Map a model-space point back to
    original-frame coords with:  x_orig = (x_model - pad_x) / ratio.
    """
    h, w = frame.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), color, dtype=np.uint8)
    dw, dh = (size - nw) // 2, (size - nh) // 2
    canvas[dh:dh + nh, dw:dw + nw] = resized
    return canvas, r, dw, dh


def to_blob(canvas: np.ndarray) -> np.ndarray:
    """640x640 letterboxed BGR uint8 -> NCHW float32 0..1 RGB blob."""
    blob = cv2.dnn.blobFromImage(canvas, 1.0 / 255.0, swapRB=True, crop=False)
    return np.ascontiguousarray(blob, dtype=np.float32)
