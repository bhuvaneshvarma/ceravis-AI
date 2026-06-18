from __future__ import annotations

import cv2
import numpy as np


def annotate_event_frame(
    frame: np.ndarray,
    bbox: tuple[float, float, float, float] | None,
    caption: str,
    box_label: str | None = None,
    color: tuple[int, int, int] = (0, 200, 120),
) -> np.ndarray:
    """
    Draw a single labeled box + a top caption on a COPY of the frame (BGR).

    Used to produce the annotated snapshot saved with an event, so the operator
    sees exactly what/where it happened. Never mutates the input frame.
    """
    img = frame.copy()
    if bbox is not None:
        x1, y1, x2, y2 = (int(v) for v in bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        if box_label:
            cv2.putText(img, box_label, (x1, max(14, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    if caption:
        # readable caption strip at the top-left
        cv2.rectangle(img, (0, 0), (min(img.shape[1], 14 + 11 * len(caption)), 30),
                      (0, 0, 0), -1)
        cv2.putText(img, caption, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 220, 255), 2)
    return img
