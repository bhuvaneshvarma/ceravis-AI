from __future__ import annotations

import logging

import cv2
import numpy as np

from config.settings import settings
from detection.trt_engine import TensorRTEngine


logger = logging.getLogger("reid")

# FastReid (BoT, AGW, SBS configs) uses pixel-scale BGR inputs with
# ImageNet mean/std on a 256x128 tensor (height x width).
#   reference: facebookresearch/fast-reid Datasets/transforms/build.py
_PIXEL_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32).reshape(1, 1, 3)
_PIXEL_STD = np.array([58.395, 57.12, 57.375], dtype=np.float32).reshape(1, 1, 3)


class FastReidExtractor:
    """
    FastReid (BoT_R50 by default) TensorRT embedding extractor.

    Input crop:  BGR uint8, any size — resized to (W=128, H=256).
    Output:      float32 L2-normalized embedding of dim
                 settings.reid_embedding_dim (2048 for R50).
    """

    def __init__(self) -> None:
        self._h = settings.reid_input_height
        self._w = settings.reid_input_width
        self._engine = TensorRTEngine(settings.reid_model_path)
        logger.info(
            "FastReidExtractor initialized (%dx%d, dim=%d)",
            self._h, self._w, settings.reid_embedding_dim,
        )

    def _preprocess(self, crop: np.ndarray) -> np.ndarray:
        # FastReid keeps BGR — do NOT swapRB.
        img = cv2.resize(crop, (self._w, self._h), interpolation=cv2.INTER_LINEAR)
        img = img.astype(np.float32)
        img = (img - _PIXEL_MEAN) / _PIXEL_STD
        img = img.transpose(2, 0, 1)[None, ...]   # HWC -> NCHW
        return np.ascontiguousarray(img, dtype=np.float32)

    def embed(self, crop: np.ndarray) -> np.ndarray:
        """Returns L2-normalized embedding (float32), zeros if crop is junk."""
        if crop is None or crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 8:
            return np.zeros(settings.reid_embedding_dim, dtype=np.float32)
        outputs = self._engine.infer(self._preprocess(crop))
        emb = outputs[0].ravel().astype(np.float32)
        norm = np.linalg.norm(emb) + 1e-9
        return emb / norm

    def embed_batch(self, crops: list[np.ndarray]) -> np.ndarray:
        """Convenience: stack of N embeddings (N, dim) — for enrollment."""
        if not crops:
            return np.zeros((0, settings.reid_embedding_dim), dtype=np.float32)
        out = np.stack([self.embed(c) for c in crops], axis=0)
        return out
