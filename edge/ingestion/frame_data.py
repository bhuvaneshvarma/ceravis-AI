from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np


@dataclass(slots=True)
class FrameData:
    """
    The latest frame of one camera.

    `image` is the picture as the reader delivered it: from the hardware
    decoder, 4-channel BGRx straight out of the GPU's colour converter (no CPU
    colour pass over the whole frame); from a software fallback, BGR. The hot
    paths — detection and pose (letterbox resizes it first), the person crops
    (common.crops) — read `image` and take three channels of only what they
    use. `frame` is the whole picture as BGR, converted on first use and kept,
    for the occasional whole-frame reader (snapshots, stills, the proof frame).
    """

    camera_id: str

    image: np.ndarray

    frame_id: int

    timestamp: datetime

    width: int

    height: int

    fps: float

    _bgr: np.ndarray | None = None

    @property
    def frame(self) -> np.ndarray:
        if self._bgr is None:
            img = self.image
            self._bgr = (cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                         if img.ndim == 3 and img.shape[2] == 4 else img)
        return self._bgr
