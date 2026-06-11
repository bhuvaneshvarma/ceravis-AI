from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(slots=True)
class FrameData:
    """
    Latest frame metadata.
    """

    camera_id: str

    frame: np.ndarray

    frame_id: int

    timestamp: datetime

    width: int

    height: int

    fps: float