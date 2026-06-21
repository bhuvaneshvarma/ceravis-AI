"""
Render sample annotated snapshots to eyeball the overlay style (no TRT needed).

Run:  PYTHONPATH=edge python edge/scripts/preview_annotation.py [out_dir]
"""
import sys
from pathlib import Path

import cv2
import numpy as np

from common.annotate import annotate_event_frame


def _scene(w=1280, h=720) -> np.ndarray:
    # a soft vertical gradient stands in for a real room frame
    top, bot = np.array([60, 50, 45]), np.array([120, 110, 100])
    grad = (np.linspace(0, 1, h)[:, None, None] * (bot - top) + top).astype(np.uint8)
    img = np.repeat(grad, w, axis=1)
    cv2.rectangle(img, (0, h - 120), (w, h), (40, 35, 32), -1)   # "floor"
    return img


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    samples = [
        ("critical", "Fall detected", "Kitchen, Living room",
         "resident-01", (470, 380, 760, 690), "fall_critical.jpg"),
        ("warning", "Visitor arrived", "Front room", None,
         (520, 180, 700, 600), "visitor_warning.jpg"),
        ("info", "Prolonged sitting", "Bedroom", "resident-01",
         (560, 240, 740, 640), "sitting_info.jpg"),
    ]
    for sev, title, loc, subj, box, name in samples:
        img = annotate_event_frame(
            _scene(), box, timestamp_text="2026-06-22  14:30:45 UTC",
            title=title, location=loc, subject=subj, severity=sev)
        cv2.imwrite(str(out / name), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print("wrote", out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
