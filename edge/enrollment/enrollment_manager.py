from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

from config.settings import settings
from common import clock
from ingestion.illumination import Modality


logger = logging.getLogger("enrollment")

# edge/ project root (this file is edge/enrollment/enrollment_manager.py).
# Used to anchor a relative data_dir so recipient media/embeddings live in the
# same place regardless of the process working directory.
_EDGE_ROOT = Path(__file__).resolve().parents[1]

# The gallery's stores, per modality: (vectors file, aligned labels file).
# Colour is the original pair — embeddings.npy is the file the cloud keeps
# (uploadEmbeddingFile) and is untouched by night vision. The infrared pair is
# derived on the device: the IR copy of every enrollment crop, plus the real
# infrared looks learned live at night. Both are regenerable, so neither leaves
# the device.
_ENROLLED = {Modality.COLOR.value: ("embeddings.npy", "labels_emb.json"),
             Modality.IR.value: ("embeddings_ir.npy", "labels_emb.json")}
_ADAPTIVE = {Modality.COLOR.value: ("adaptive.npy", "adaptive_labels.json"),
             Modality.IR.value: ("adaptive_ir.npy", "adaptive_ir_labels.json")}


def _load_vectors(f: Path | None, dim: int | None = None) -> np.ndarray:
    """A stored (K, dim) vector file, or an empty (0, dim) array when there is
    none, it cannot be read, or it was made by a different model (another
    width). The ONE loader for enrolled, adaptive and face vectors alike: a
    vector from another model is not a vector of this one, and mixing widths
    made the gallery rebuild throw — leaving the AI silently off after a model
    change. `dim` defaults to the body ReID width."""
    dim = dim or settings.reid_embedding_dim
    empty = np.zeros((0, dim), dtype=np.float32)
    if f is None or not f.exists():
        return empty
    try:
        arr = np.load(f).astype(np.float32)
    except Exception:
        logger.warning("enroll: cannot read %s — ignored", f)
        return empty
    if arr.ndim != 2 or arr.shape[0] == 0:
        return empty
    if arr.shape[1] != dim:
        logger.warning("enroll: %s holds %d-wide vectors from a different ReID "
                       "model (this one is %d) — ignored until re-embedded",
                       f, arr.shape[1], dim)
        return empty
    return arr


class EnrollmentManager:
    """
    On-disk store for per-recipient enrollment media + embeddings.

    Layout:
        data/recipients/<recipient_id>/
            photos/        uploaded / live-captured images
            videos/        enrollment videos
            body/          embeddings.npy  (K, dim) ReID embeddings (colour)
                           embeddings_ir.npy  the same crops, infrared view
                           adaptive[_ir].npy  live-learned looks per modality
            face/          embeddings.npy  (K, 512) enrolled faces (AuraFace)
            status.json    enrollment job state
    """

    SUBDIRS = ("photos", "videos", "face", "body")

    def __init__(self) -> None:
        data_root = settings.data_path
        if not data_root.is_absolute():
            data_root = _EDGE_ROOT / data_root
        self.base_path = data_root / "recipients"
        self.base_path.mkdir(parents=True, exist_ok=True)

    # ---- folders -----------------------------------------------------
    def create_recipient_folder(self, recipient_id: str) -> Path:
        root = self.base_path / recipient_id
        root.mkdir(exist_ok=True)
        for sub in self.SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
        return root

    def get_recipient_folder(self, recipient_id: str) -> Path | None:
        path = self.base_path / recipient_id
        return path if path.exists() else None

    # ---- media ingest ------------------------------------------------
    def save_photo(self, recipient_id: str, data: bytes, ext: str = "jpg") -> Path:
        root = self.create_recipient_folder(recipient_id)
        name = f"{int(time.time() * 1000)}.{ext.lstrip('.').lower()}"
        path = root / "photos" / name
        path.write_bytes(data)
        return path

    def import_photo(self, recipient_id: str, name: str, data: bytes) -> Path:
        """Save an externally-sourced photo (e.g. a cloud posture image) under a
        STABLE basename, so re-importing the same source overwrites in place
        instead of piling up duplicates. Path-traversal safe."""
        root = self.create_recipient_folder(recipient_id)
        path = root / "photos" / Path(name).name
        path.write_bytes(data)
        return path

    def has_photo(self, recipient_id: str, name: str) -> bool:
        root = self.get_recipient_folder(recipient_id)
        return bool(root and (root / "photos" / Path(name).name).exists())

    def save_video(self, recipient_id: str, data: bytes, ext: str = "mp4") -> Path:
        root = self.create_recipient_folder(recipient_id)
        name = f"{int(time.time() * 1000)}.{ext.lstrip('.').lower()}"
        path = root / "videos" / name
        path.write_bytes(data)
        return path

    def list_photos(self, recipient_id: str) -> list[Path]:
        root = self.get_recipient_folder(recipient_id)
        if root is None:
            return []
        return sorted((root / "photos").glob("*"))

    def media_names(self, recipient_id: str) -> list[str]:
        """Filenames of stored enrollment photos (for UI preview)."""
        return [p.name for p in self.list_photos(recipient_id)]

    # ---- frame labels (viewpoint/posture tags) ----------------------
    def get_labels(self, recipient_id: str) -> dict[str, str]:
        """filename -> label (e.g. 'front/standing') for captured frames."""
        root = self.get_recipient_folder(recipient_id)
        path = root / "labels.json" if root else None
        if path and path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return {}
        return {}

    def record_label(self, recipient_id: str, filename: str, label: str) -> None:
        """Tag a captured frame; additive, never touches the image itself."""
        if not label:
            return
        root = self.create_recipient_folder(recipient_id)
        labels = self.get_labels(recipient_id)
        labels[filename] = label
        (root / "labels.json").write_text(json.dumps(labels, indent=2))

    def media_path(self, recipient_id: str, name: str) -> Path | None:
        """Resolve a stored photo/crop by basename — path-traversal safe."""
        root = self.get_recipient_folder(recipient_id)
        if root is None:
            return None
        safe = Path(name).name                       # strip any directory parts
        for sub in ("photos", "body/crops"):
            p = root / sub / safe
            if p.exists():
                return p
        return None

    def save_reference_crops(self, recipient_id: str,
                             crops: list[np.ndarray], limit: int = 6) -> int:
        """Persist a few person crops as small JPEGs for future reference."""
        import cv2
        root = self.create_recipient_folder(recipient_id)
        out = root / "body" / "crops"
        out.mkdir(parents=True, exist_ok=True)
        for f in out.glob("*.jpg"):
            f.unlink()
        saved = 0
        step = max(1, len(crops) // limit)
        for i, crop in enumerate(crops[::step][:limit]):
            if crop is None or crop.size == 0:
                continue
            cv2.imwrite(str(out / f"ref_{saved:02d}.jpg"), crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 80])
            saved += 1
        return saved

    def list_videos(self, recipient_id: str) -> list[Path]:
        root = self.get_recipient_folder(recipient_id)
        if root is None:
            return []
        return sorted((root / "videos").glob("*"))

    # ---- embeddings --------------------------------------------------
    def save_embeddings(self, recipient_id: str, embeddings: np.ndarray,
                        modality: str = Modality.COLOR.value) -> None:
        """Persist this recipient's embeddings (overwrites — worker passes the
        full set it computed for the recipient)."""
        root = self.create_recipient_folder(recipient_id)
        np.save(root / "body" / _ENROLLED[modality][0],
                embeddings.astype(np.float32))

    def load_embeddings(self, recipient_id: str,
                        modality: str = Modality.COLOR.value) -> np.ndarray:
        root = self.get_recipient_folder(recipient_id)
        f = root / "body" / _ENROLLED[modality][0] if root else None
        return _load_vectors(f)

    def stale_model(self, recipient_id: str) -> bool:
        """Enrolled vectors exist but were made by a DIFFERENT ReID model (their
        width is not this model's). They can never be matched against, so the
        recipient needs re-embedding from their stored media — unlike a gallery
        that was removed on purpose, where there is no file at all."""
        root = self.get_recipient_folder(recipient_id)
        f = root / "body" / _ENROLLED[Modality.COLOR.value][0] if root else None
        try:
            if not (f and f.exists()):
                return False
            arr = np.load(f, mmap_mode="r")
            return arr.ndim == 2 and arr.shape[1] != settings.reid_embedding_dim
        except Exception:
            return False

    # ---- face vectors (the second identity cue, reid/face_identity.py) ----
    def save_face_embeddings(self, recipient_id: str, faces: np.ndarray) -> None:
        """The recipient's enrolled faces, (K, 128) unit vectors, in face/."""
        root = self.create_recipient_folder(recipient_id)
        np.save(root / "face" / "embeddings.npy", faces.astype(np.float32))

    def has_face_embeddings(self, recipient_id: str) -> bool:
        """Faces from THIS face model exist (possibly zero rows: enrolled, no
        clear face). A file from another model (another width, e.g. the 128-d
        SFace era) does not count, so it is re-embedded from the photos."""
        from reid.face_identity import FACE_DIM
        root = self.get_recipient_folder(recipient_id)
        f = root / "face" / "embeddings.npy" if root else None
        try:
            return bool(f and f.exists()
                        and np.load(f, mmap_mode="r").shape[-1] == FACE_DIM)
        except Exception:
            return False

    def load_face_gallery(self) -> dict[str, np.ndarray]:
        """recipient_id -> (K, 128) enrolled faces, for every recipient."""
        from reid.face_identity import FACE_DIM
        out: dict[str, np.ndarray] = {}
        for root in sorted(self.base_path.glob("*")):
            if root.is_dir():
                arr = _load_vectors(root / "face" / "embeddings.npy", FACE_DIM)
                if arr.shape[0]:
                    out[root.name] = arr
        return out

    def save_embedding_labels(self, recipient_id: str, labels: list[str]) -> None:
        """Per-embedding labels aligned to embeddings.npy rows — metadata for
        future pose/action analytics; not used for matching. Best-effort."""
        root = self.create_recipient_folder(recipient_id)
        (root / "body" / "labels_emb.json").write_text(json.dumps(list(labels)))

    # ---- adaptive (online-learning) embeddings ----------------------
    # Vectors captured live while the target is matched with high confidence.
    # Stored SEPARATELY from the enrolled set (which is never overwritten),
    # capped FIFO (newest kept), and included in the gallery so they help match
    # the recipient through appearance/clothing changes.
    def load_adaptive(self, recipient_id: str,
                      modality: str = Modality.COLOR.value) -> np.ndarray:
        root = self.get_recipient_folder(recipient_id)
        f = root / "body" / _ADAPTIVE[modality][0] if root else None
        return _load_vectors(f)

    def _load_adaptive_labels(self, recipient_id: str,
                              modality: str = Modality.COLOR.value) -> list[str]:
        root = self.get_recipient_folder(recipient_id)
        f = root / "body" / _ADAPTIVE[modality][1] if root else None
        if f and f.exists():
            try:
                return list(json.loads(f.read_text()))
            except Exception:
                return []
        return []

    def append_adaptive(self, recipient_id: str, embedding: np.ndarray,
                        label: str = "", *, cap: int, dedup_cos: float,
                        modality: str = Modality.COLOR.value) -> bool:
        """
        Add a live embedding to the recipient's adaptive store if it is novel
        (max cosine vs existing enrolled+adaptive < dedup_cos). FIFO-evicts the
        oldest beyond `cap`. Returns True if it was added (so the caller can
        rebuild the gallery). Writes are atomic; enrolled vectors are untouched.
        `modality` picks the store — an infrared look is compared with, and
        filed among, infrared looks only.
        """
        emb = np.asarray(embedding, dtype=np.float32).ravel()
        norm = float(np.linalg.norm(emb))
        if emb.shape[0] != settings.reid_embedding_dim or norm == 0.0:
            return False
        emb = emb / norm

        enrolled = self.load_embeddings(recipient_id, modality)
        adaptive = self.load_adaptive(recipient_id, modality)
        existing = [a for a in (enrolled, adaptive) if a.ndim == 2 and a.shape[0]]
        if existing:
            stack = np.concatenate(existing, axis=0)
            if float(np.max(stack @ emb)) >= dedup_cos:
                return False                      # too similar — no new info

        adaptive = (np.concatenate([adaptive, emb[None, :]], axis=0)
                    if adaptive.shape[0] else emb[None, :])
        labels = self._load_adaptive_labels(recipient_id, modality)
        labels.append(label)
        if adaptive.shape[0] > cap:
            adaptive, labels = self._prune_redundant(adaptive, labels, cap)

        vec_name, label_name = _ADAPTIVE[modality]
        body = self.create_recipient_folder(recipient_id) / "body"
        body.mkdir(parents=True, exist_ok=True)
        tmp = body / f"{vec_name}.tmp"
        with open(tmp, "wb") as fh:               # atomic save (temp + replace)
            np.save(fh, adaptive.astype(np.float32))
        os.replace(tmp, body / vec_name)
        (body / label_name).write_text(json.dumps(labels))
        return True

    @staticmethod
    def _prune_redundant(vecs: np.ndarray, labels: list[str],
                         cap: int) -> tuple[np.ndarray, list[str]]:
        """Keep a DIVERSE set: when over cap, drop the most redundant vector
        (the one nearest another), not the oldest — so distinct appearances
        (e.g. a different outfit) are retained instead of FIFO-evicted, and the
        recipient still matches if they re-wear an old outfit days later."""
        labels = list(labels)
        while vecs.shape[0] > cap:
            sim = vecs @ vecs.T                   # cosine (rows are L2-normalized)
            np.fill_diagonal(sim, -1.0)
            drop = int(np.argmax(sim.max(axis=1)))    # most redundant vector
            keep = [i for i in range(vecs.shape[0]) if i != drop]
            vecs = vecs[keep]
            labels = [labels[i] for i in keep]
        return vecs, labels

    def embedding_stats(self, recipient_id: str) -> dict:
        """Inspect what's stored / being captured for a recipient — so live
        adaptive capture is observable (counts, last capture time, labels)."""
        enrolled = self.load_embeddings(recipient_id)
        adaptive = self.load_adaptive(recipient_id)
        labels = self._load_adaptive_labels(recipient_id)
        root = self.get_recipient_folder(recipient_id)
        af = root / "body" / "adaptive.npy" if root else None
        last = None
        if af and af.exists():
            last = datetime.fromtimestamp(af.stat().st_mtime,
                                          clock.local_tz()).isoformat()
        ir = Modality.IR.value
        return {
            "enrolled": int(enrolled.shape[0]),
            "adaptive": int(adaptive.shape[0]),
            # Night vision: the IR copy of the enrollment set, and the real
            # infrared looks learned so far (grows night by night).
            "enrolled_ir": int(self.load_embeddings(recipient_id, ir).shape[0]),
            "adaptive_ir": int(self.load_adaptive(recipient_id, ir).shape[0]),
            "adaptive_cap": settings.reid_adaptive_max,
            "last_adaptive_capture": last,
            "adaptive_labels": dict(Counter(lbl for lbl in labels if lbl)),
        }

    def _labels_aligned(self, root: Path, fname: str, n: int) -> list[str]:
        """Read a labels json and pad/truncate to exactly n entries."""
        f = root / "body" / fname
        out: list[str] = []
        if f.exists():
            try:
                out = [str(x) for x in json.loads(f.read_text())]
            except Exception:
                out = []
        if len(out) < n:
            out += [""] * (n - len(out))
        return out[:n]

    def load_gallery(self) -> tuple[np.ndarray, list[str], list[str], list[str]]:
        """Concatenate every recipient's enrolled + adaptive embeddings, colour
        and infrared, with parallel recipient-id, view/pose-label and modality
        lists (FaissGallery.rebuild takes all four)."""
        all_emb: list[np.ndarray] = []
        ids: list[str] = []
        labels: list[str] = []
        mods: list[str] = []
        stores = ([(m, *_ENROLLED[m]) for m in _ENROLLED]
                  + [(m, *_ADAPTIVE[m]) for m in _ADAPTIVE])
        for root in sorted(self.base_path.glob("*")):
            if not root.is_dir():
                continue
            for mod, fname, lname in stores:
                arr = _load_vectors(root / "body" / fname)
                if arr.shape[0] > 0:
                    all_emb.append(arr)
                    ids.extend([root.name] * arr.shape[0])
                    labels.extend(self._labels_aligned(root, lname, arr.shape[0]))
                    mods.extend([mod] * arr.shape[0])
        if not all_emb:
            return (np.zeros((0, settings.reid_embedding_dim), dtype=np.float32),
                    [], [], [])
        return np.concatenate(all_emb, axis=0), ids, labels, mods

    # ---- status ------------------------------------------------------
    def set_status(self, recipient_id: str, **fields) -> None:
        root = self.create_recipient_folder(recipient_id)
        path = root / "status.json"
        cur = self.get_status(recipient_id)
        cur.update(fields)
        cur["updated"] = clock.now_iso()      # was naive: no offset, unlike every
                                      # other timestamp the device writes
        path.write_text(json.dumps(cur, indent=2))

    def get_status(self, recipient_id: str) -> dict:
        root = self.get_recipient_folder(recipient_id)
        path = root / "status.json" if root else None
        if path and path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return {"state": "none", "photos": 0, "embeddings": 0, "message": ""}
