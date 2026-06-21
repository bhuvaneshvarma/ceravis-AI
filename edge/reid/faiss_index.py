from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np

from config.settings import settings

try:
    import faiss  # type: ignore
    _FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore
    _FAISS_AVAILABLE = False


logger = logging.getLogger("reid")


@dataclass(slots=True, frozen=True)
class MatchResult:
    """Outcome of a hybrid set-to-set gallery match."""
    recipient_id: str | None
    score: float            # fused score of the best recipient
    view_label: str | None
    margin: float           # best score minus runner-up recipient's score
    is_match: bool          # passed threshold + margin + vote gates


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
        self._labels: list[str] = []     # per-vector view/pose label (parallel to _ids)
        # Raw normalized vectors + per-recipient row index, kept for the hybrid
        # set-to-set match (FAISS top-1 alone can be fooled by a single lucky
        # vector). The gallery is small (≤ a few hundred), so a full grouped
        # scan is exact and trivially cheap.
        self._emb: np.ndarray = np.zeros((0, self._dim), dtype=np.float32)
        self._id_rows: dict[str, list[int]] = {}

    # ---- build -------------------------------------------------------
    def rebuild(
        self,
        embeddings: np.ndarray,  # (N, dim) float32, L2-normalized
        recipient_ids: list[str],
        labels: list[str] | None = None,   # per-vector view/pose label (optional)
    ) -> None:
        if embeddings.ndim != 2 or embeddings.shape[1] != self._dim:
            raise ValueError(f"Expected (N,{self._dim}) embeddings")
        new_index = faiss.IndexFlatIP(self._dim)
        if embeddings.size > 0:
            new_index.add(embeddings.astype(np.float32))
        new_labels = (list(labels) if labels is not None
                      else [""] * len(recipient_ids))
        new_emb = (embeddings.astype(np.float32).copy() if embeddings.size > 0
                   else np.zeros((0, self._dim), dtype=np.float32))
        new_rows: dict[str, list[int]] = {}
        for i, rid in enumerate(recipient_ids):
            new_rows.setdefault(rid, []).append(i)
        with self._swap_lock:
            self._index = new_index
            self._ids = list(recipient_ids)
            self._labels = new_labels
            self._emb = new_emb
            self._id_rows = new_rows
        logger.info("FAISS gallery rebuilt: %d entries", len(recipient_ids))

    # ---- query -------------------------------------------------------
    def search(
        self,
        embedding: np.ndarray,
        top_k: int = 1,
    ) -> tuple[str | None, float, str | None]:
        # Hold references locally so an atomic swap can't race us
        index = self._index
        ids = self._ids
        labels = self._labels
        if index.ntotal == 0:
            return None, 0.0, None
        q = embedding.reshape(1, -1).astype(np.float32)
        scores, idxs = index.search(q, top_k)
        i = int(idxs[0, 0])
        if i < 0 or i >= len(ids):
            return None, 0.0, None
        label = labels[i] if i < len(labels) else None
        return ids[i], float(scores[0, 0]), (label or None)

    # ---- hybrid set-to-set match -------------------------------------
    def match(
        self,
        embedding: np.ndarray,
        *,
        alpha: float | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
        margin: float | None = None,
        min_votes: int | None = None,
        vote_floor: float | None = None,
    ) -> MatchResult:
        """
        Score the query against EVERY enrolled+adaptive vector of each recipient,
        aggregate per recipient, and require a confident, unambiguous winner.

        Per recipient r with vectors {gᵢ}:
            score(r) = alpha·max_i cos(q, gᵢ) + (1-alpha)·mean(top-k cos(q, gᵢ))
        A match needs all three:
            * score(best) ≥ threshold        (strong enough overall)
            * score(best) - score(2nd) ≥ margin   (clearly one person, not a tie)
            * #vectors of best with cos ≥ vote_floor ≥ min_votes  (majority agree)
        This is far steadier than a single top-1 vector, and is what you asked
        for: lock on by agreement of the whole stored set, at that moment.
        """
        alpha = settings.reid_hybrid_alpha if alpha is None else alpha
        top_k = settings.reid_hybrid_top_k if top_k is None else top_k
        threshold = settings.reid_match_threshold if threshold is None else threshold
        margin = settings.reid_match_margin if margin is None else margin
        min_votes = settings.reid_hybrid_min_votes if min_votes is None else min_votes
        vote_floor = settings.reid_hybrid_vote_floor if vote_floor is None else vote_floor

        # Snapshot references so an atomic rebuild can't race us.
        emb, id_rows, labels = self._emb, self._id_rows, self._labels
        if emb.shape[0] == 0:
            return MatchResult(None, 0.0, None, 0.0, False)

        q = np.asarray(embedding, dtype=np.float32).ravel()
        n = np.linalg.norm(q)
        if n == 0.0 or q.shape[0] != emb.shape[1]:
            return MatchResult(None, 0.0, None, 0.0, False)
        q = q / n
        sims = emb @ q                                  # cosine to every vector

        scored: list[tuple[float, str, int, int]] = []  # (score, rid, best_row, votes)
        for rid, rows in id_rows.items():
            r_sims = sims[rows]
            s_max = float(r_sims.max())
            k = min(top_k, r_sims.shape[0])
            s_topk = float(np.mean(np.sort(r_sims)[-k:]))
            score = alpha * s_max + (1.0 - alpha) * s_topk
            votes = int(np.count_nonzero(r_sims >= vote_floor))
            best_row = rows[int(np.argmax(r_sims))]
            scored.append((score, rid, best_row, votes))

        scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best_rid, best_row, best_votes = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        gap = best_score - second
        is_match = (best_score >= threshold and gap >= margin
                    and best_votes >= min_votes)
        view = labels[best_row] if best_row < len(labels) else None
        return MatchResult(best_rid, best_score, (view or None), gap, is_match)

    @property
    def size(self) -> int:
        return self._index.ntotal
