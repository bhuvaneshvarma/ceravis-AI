from __future__ import annotations

import logging
import threading

import numpy as np

from config.settings import settings

try:
    import faiss  # type: ignore
    _FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore
    _FAISS_AVAILABLE = False


logger = logging.getLogger("reid")


class FaissGallery:
    """
    L2-normalized embedding gallery with atomic-swap rebuilds.

    Why two indexes:
      - Read path (query) holds a stable reference.
      - Write path (enrollment) builds a new index and atomically swaps.
      - Lock only guards the reference rebind, not the search.
    """

    def __init__(self, dim: int | None = None) -> None:
        if not _FAISS_AVAILABLE:
            raise RuntimeError("faiss not installed")
        self._dim = dim or settings.reid_embedding_dim
        self._swap_lock = threading.Lock()
        self._index: "faiss.Index" = faiss.IndexFlatIP(self._dim)
        self._ids: list[str] = []

    # ---- build -------------------------------------------------------
    def rebuild(
        self,
        embeddings: np.ndarray,  # (N, dim) float32, L2-normalized
        recipient_ids: list[str],
    ) -> None:
        if embeddings.ndim != 2 or embeddings.shape[1] != self._dim:
            raise ValueError(f"Expected (N,{self._dim}) embeddings")
        new_index = faiss.IndexFlatIP(self._dim)
        if embeddings.size > 0:
            new_index.add(embeddings.astype(np.float32))
        with self._swap_lock:
            self._index = new_index
            self._ids = list(recipient_ids)
        logger.info("FAISS gallery rebuilt: %d entries", len(recipient_ids))

    # ---- query -------------------------------------------------------
    def search(
        self,
        embedding: np.ndarray,
        top_k: int = 1,
    ) -> tuple[str | None, float]:
        # Hold reference locally so atomic swap can't race us
        index = self._index
        ids = self._ids
        if index.ntotal == 0:
            return None, 0.0
        q = embedding.reshape(1, -1).astype(np.float32)
        scores, idxs = index.search(q, top_k)
        i = int(idxs[0, 0])
        if i < 0 or i >= len(ids):
            return None, 0.0
        return ids[i], float(scores[0, 0])

    @property
    def size(self) -> int:
        return self._index.ntotal
