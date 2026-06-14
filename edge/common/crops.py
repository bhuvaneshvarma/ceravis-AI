from __future__ import annotations

import numpy as np


def crop_person(frame: np.ndarray, x1: float, y1: float, x2: float, y2: float,
                pad_frac: float = 0.08) -> tuple[np.ndarray, int, int]:
    """
    Crop a padded person region from a frame.

    Padding (a small margin around the detection box, ~pad_frac of box size)
    keeps the limbs/feet that detectors often clip — important for both ReID
    embeddings and pose. Only this crop is handed downstream; the surrounding
    pixels are dropped, which is what keeps ReID/pose cheap.

    Returns (crop, x_offset, y_offset) so keypoints found in crop space can be
    mapped back to full-frame coordinates: frame_x = crop_x + x_offset.
    """
    h, w = frame.shape[:2]
    bw, bh = (x2 - x1), (y2 - y1)
    px, py = bw * pad_frac, bh * pad_frac
    cx1 = max(0, int(x1 - px))
    cy1 = max(0, int(y1 - py))
    cx2 = min(w, int(x2 + px))
    cy2 = min(h, int(y2 + py))
    if cx2 <= cx1 or cy2 <= cy1:
        return np.empty((0, 0, 3), dtype=frame.dtype), 0, 0
    return frame[cy1:cy2, cx1:cx2], cx1, cy1
