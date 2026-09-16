from __future__ import annotations

"""
Recording proof frames — one annotated still per clip, for false-positive triage.

The recording archive answers "was anyone here"; this answers "what did YOLO
think it saw the instant it decided to record", without scrubbing video. When a
clip OPENS, the frame that triggered it is saved with every person box drawn
(and its confidence), so a night of phantom clips can be audited at a glance —
an empty office, a green box on a white chair, and the number that let it
through are all right there in one image.

A pure add-on: it never blocks or fails the recorder (best-effort, swallows
everything), holds no frames, and self-expires on the SAME horizon as the clips
it documents, so it can be deleted wholesale with no other change.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from common import clock
from config.settings import settings

try:
    import cv2
    _HAVE_CV2 = True
except ImportError:                                  # pragma: no cover
    cv2 = None                                       # type: ignore
    _HAVE_CV2 = False


logger = logging.getLogger("media")

_EDGE_ROOT = Path(__file__).resolve().parents[1]
_SWEEP_EVERY_SECS = 600.0                             # housekeeping cadence


def _proof_root() -> Path:
    base = settings.data_path
    base = base if base.is_absolute() else (_EDGE_ROOT / base)
    return base / "recordings_proof"


class ProofWriter:
    """Saves an annotated still when a recording opens, and prunes old ones."""

    def __init__(self, frame_buffer) -> None:
        self._frames = frame_buffer
        self._last_sweep = 0.0

    # ---- capture -----------------------------------------------------
    def capture(self, camera_id: str, result) -> None:
        """Save the current frame with `result`'s person boxes drawn. Best-effort
        — a proof frame is never worth interrupting a recording for."""
        if not settings.record_proof_frames or not _HAVE_CV2 or self._frames is None:
            return
        try:
            fd = self._frames.get(camera_id)
            if fd is None:
                return
            img = fd.frame.copy()
            for d in result.detections:
                b = d.bbox
                p1 = (int(b.x1), int(b.y1))
                p2 = (int(b.x2), int(b.y2))
                cv2.rectangle(img, p1, p2, (0, 255, 0), 2)
                cv2.putText(img, f"person {d.confidence:.2f}",
                            (int(b.x1), max(0, int(b.y1) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            now = clock.now()
            cv2.putText(img, f"REC start {now.strftime('%Y-%m-%d %H:%M:%S')} "
                             f"{camera_id} · {len(result.detections)} person(s)",
                        (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            out = (_proof_root() / settings.device_id
                   / now.strftime("%Y-%m-%d")
                   / f"{camera_id}_{now.strftime('%H%M%S_%f')[:-3]}.jpg")
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out), img,
                        [cv2.IMWRITE_JPEG_QUALITY, int(settings.event_snapshot_quality)])
        except Exception:
            logger.exception("recording proof frame failed camera=%s", camera_id)

    # ---- housekeeping ------------------------------------------------
    def sweep(self) -> None:
        """Delete proof stills past the recording retention window. Throttled, so
        it is safe to call every recording tick."""
        now = time.monotonic()
        if (now - self._last_sweep) < _SWEEP_EVERY_SECS:
            return
        self._last_sweep = now
        if not settings.record_proof_frames:
            return
        root = _proof_root()
        if not root.is_dir():
            return
        cutoff = time.time() - settings.record_retention_hours * 3600
        try:
            for jpg in root.rglob("*.jpg"):
                try:
                    if jpg.stat().st_mtime < cutoff:
                        jpg.unlink()
                except OSError:
                    continue
            # drop now-empty day/device folders so the tree doesn't accrete
            for d in sorted(root.rglob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()
                    except OSError:
                        pass
        except Exception:
            logger.exception("recording proof sweep failed")
