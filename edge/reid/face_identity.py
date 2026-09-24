from __future__ import annotations

"""
Face identity — the second, clothing-independent cue next to body ReID.

Body ReID matches mostly on clothes, so it cannot tell two people in similar
jeans and shirts apart, and it loses the recipient when they change clothes.
The face does neither. This module finds a face in the upper body of a person
box (YuNet: detection + 5 landmarks, MIT licence, the device's OpenCV), aligns
it to the standard ArcFace template and embeds it (AuraFace: ResNet100 ArcFace,
512-d, Apache-2.0, TensorRT on the GPU), and scores it against the recipient's
enrolled faces — only for the few tracks where identity is actually in
question (see TrackingRunner._maybe_face).

It is EVIDENCE for the one lock decision in reid/target_lock.py, not a second
identity mechanism: a clear match confirms a body match, a clear mismatch
vetoes it. Without the model files it switches itself off and the body path
runs exactly as before.
"""

import logging
from pathlib import Path

import cv2
import numpy as np

from common.crops import crop_person
from config.settings import settings


logger = logging.getLogger("reid")

_EDGE_ROOT = Path(__file__).resolve().parents[1]
FACE_DIM = 512
# The ArcFace 112x112 template the 5 landmarks are aligned to (eyes, nose, mouth
# corners, in YuNet's order). Checked on the bench: embeddings from this
# alignment equal OpenCV's own alignCrop to cosine >= 0.9999.
_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                      [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)
_UPPER_FRAC = 0.55     # a person's face is in the upper body of their box
_MIN_REGION_PX = 64    # smaller regions hold no usable face (and trip YuNet 2022mar)
_LIVE_MAX_PX = 640     # YuNet input bound for live regions (CPU cost)
_PHOTO_MAX_PX = 1280   # enrollment photos: a 2560 px frame keeps a ~45 px face


def _path(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else _EDGE_ROOT / q


def _similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (Umeyama) mapping src -> dst, 2x3."""
    ms, md = src.mean(0), dst.mean(0)
    sc, dc = src - ms, dst - md
    u, sv, vt = np.linalg.svd(dc.T @ sc / len(src))
    d = np.diag([1.0, float(np.sign(np.linalg.det(u) * np.linalg.det(vt)))])
    r = u @ d @ vt
    scale = float(np.trace(np.diag(sv) @ d) / (sc ** 2).sum(1).mean())
    return np.hstack([scale * r, (md - scale * r @ ms)[:, None]]).astype(np.float32)


class FaceIdentity:
    """YuNet + AuraFace. The OpenCV detector and the TensorRT engine are used
    from one thread each: one instance per thread that uses it (the tracking
    runner and the enrollment worker each own one)."""

    def __init__(self) -> None:
        self._det = None
        self._rec = None
        if not settings.face_enabled:
            return
        det, rec = _path(settings.face_detector_path), _path(settings.face_recognizer_path)
        if not (det.exists() and rec.exists()):
            logger.warning("face identity off — model files missing (%s, %s); "
                           "run setup/export_models.py", det.name, rec.name)
            return
        try:
            from detection.trt_engine import TensorRTEngine
            self._det = cv2.FaceDetectorYN.create(str(det), "", (320, 320), 0.8, 0.3, 20)
            self._rec = TensorRTEngine(str(rec))
            logger.info("face identity ready (YuNet + AuraFace)")
        except Exception:
            logger.exception("face identity off — model load failed")
            self._det = self._rec = None

    @property
    def ready(self) -> bool:
        return self._det is not None and self._rec is not None

    def embed_person(self, frame: np.ndarray, bbox) -> tuple[np.ndarray | None, float]:
        """(unit 512-d face vector, face width in px) for the person whose box is
        `bbox` (x1, y1, x2, y2 in frame pixels), or (None, 0.0)."""
        if not self.ready:
            return None, 0.0
        x1, y1, x2, y2 = bbox
        region, _, _ = crop_person(frame, x1, y1, x2, y1 + _UPPER_FRAC * (y2 - y1), 0.15)
        if region.size == 0 or min(region.shape[:2]) < _MIN_REGION_PX:
            return None, 0.0
        faces = self._faces(region, _LIVE_MAX_PX)
        if not faces:
            return None, 0.0
        best = max(faces, key=lambda r: r[-1])
        return self._feature(region, best), float(best[2])

    def embed_photo(self, img: np.ndarray) -> tuple[np.ndarray | None, str]:
        """Enrollment: the ONE clear face in a photo -> (vector, "") or
        (None, why). A second face at least half the size is ambiguous."""
        if not self.ready:
            return None, "face models missing"
        faces = self._faces(img, _PHOTO_MAX_PX)
        if not faces:
            return None, "no face"
        faces.sort(key=lambda r: float(r[2] * r[3]), reverse=True)
        if len(faces) > 1 and faces[1][2] * faces[1][3] >= 0.5 * faces[0][2] * faces[0][3]:
            return None, "two faces"
        if faces[0][2] < settings.face_min_px:
            return None, "face too small"
        v = self._feature(img, faces[0])
        return (v, "") if v is not None else (None, "no face")

    # ---- internals ---------------------------------------------------
    def _faces(self, img: np.ndarray, max_px: int) -> list[np.ndarray]:
        """YuNet rows (box, 5 landmarks, score) in `img` pixels."""
        h, w = img.shape[:2]
        s = min(1.0, max_px / float(max(h, w)))
        small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s)))) if s < 1 else img
        try:
            self._det.setInputSize((small.shape[1], small.shape[0]))
            _, faces = self._det.detect(small)
        except cv2.error:
            return []
        if faces is None:
            return []
        out = []
        for f in faces:
            f = f.copy()
            f[:14] /= s                        # box + landmarks back to img pixels
            out.append(f)
        return out

    def _feature(self, img: np.ndarray, face: np.ndarray) -> np.ndarray | None:
        """Align the face to the ArcFace template and embed it (AuraFace)."""
        try:
            m = _similarity(face[4:14].reshape(5, 2).astype(np.float32), _TEMPLATE)
            aligned = cv2.warpAffine(img, m, (112, 112))
            x = cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB).astype(np.float32)
            x = ((x - 127.5) / 127.5).transpose(2, 0, 1)[None]
            v = np.asarray(self._rec.infer(np.ascontiguousarray(x))[0],
                           np.float32).ravel()
        except Exception:
            return None
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None


class FaceGallery:
    """recipient_id -> (K, 128) enrolled face vectors, best-cosine scoring.
    Rebuilt by the enrollment worker next to the body gallery; the swap is a
    single reference assignment, so readers never see a half-built gallery."""

    def __init__(self) -> None:
        self._g: dict[str, np.ndarray] = {}

    def rebuild(self, per_recipient: dict[str, np.ndarray]) -> None:
        self._g = {rid: a for rid, a in per_recipient.items() if a.shape[0]}
        logger.info("face gallery rebuilt: %d face(s) for %d recipient(s)",
                    self.size, len(self._g))

    @property
    def size(self) -> int:
        return sum(a.shape[0] for a in self._g.values())

    def score(self, feat: np.ndarray | None, recipient_id: str | None) -> float | None:
        """Best cosine against this recipient's faces; None when either side
        has nothing to compare."""
        g = self._g.get(recipient_id) if recipient_id else None
        if g is None or feat is None:
            return None
        return float(np.max(g @ feat))
