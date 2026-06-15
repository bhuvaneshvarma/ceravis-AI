from __future__ import annotations

import logging

import cv2
import numpy as np

from config.settings import settings
from detection.trt_engine import TensorRTEngine


logger = logging.getLogger("reid")

# ImageNet normalization in 0..1 scale. This matches both OSNet (torchreid)
# and FastReid (which writes the same constants in 0..255 scale), so one
# preprocessing path serves whichever engine is loaded.
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 1, 3)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 1, 3)


class ReIDExtractor:
    """
    Person-appearance embedding extractor (TensorRT).

    Input:  a BGR person CROP (the detector's box, padded) — never the whole
            frame, so only the person's pixels are embedded, not background.
    Output: L2-normalized float32 vector of settings.reid_embedding_dim
            (512 for OSNet x1_0, 2048 for FastReid BoT_R50).
    """

    def __init__(self) -> None:
        self._h = settings.reid_input_height
        self._w = settings.reid_input_width
        self._engine = TensorRTEngine(settings.reid_model_path)
        logger.info("ReIDExtractor ready (%dx%d, dim=%d)",
                    self._h, self._w, settings.reid_embedding_dim)

    def _preprocess(self, crop: np.ndarray) -> np.ndarray:
        img = cv2.resize(crop, (self._w, self._h), interpolation=cv2.INTER_LINEAR)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = (img - _MEAN) / _STD
        img = img.transpose(2, 0, 1)[None, ...]            # HWC -> NCHW
        return np.ascontiguousarray(img, dtype=np.float32)

    def embed(self, crop: np.ndarray) -> np.ndarray:
        if crop is None or crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 8:
            return np.zeros(settings.reid_embedding_dim, dtype=np.float32)
        out = self._engine.infer(self._preprocess(crop))
        emb = out[0].ravel().astype(np.float32)
        return emb / (np.linalg.norm(emb) + 1e-9)

    def embed_batch(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, settings.reid_embedding_dim), dtype=np.float32)
        return np.stack([self.embed(c) for c in crops], axis=0)
