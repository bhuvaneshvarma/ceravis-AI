from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from config.settings import settings


logger = logging.getLogger("enrollment")

# edge/ project root (this file is edge/enrollment/enrollment_manager.py).
# Used to anchor a relative data_dir so recipient media/embeddings live in the
# same place regardless of the process working directory.
_EDGE_ROOT = Path(__file__).resolve().parents[1]


class EnrollmentManager:
    """
    On-disk store for per-recipient enrollment media + embeddings.

    Layout:
        data/recipients/<recipient_id>/
            photos/        uploaded / live-captured images
            videos/        enrollment videos
            body/          embeddings.npy  (K, dim) ReID embeddings
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
    def save_embeddings(self, recipient_id: str, embeddings: np.ndarray) -> None:
        """Persist this recipient's embeddings (overwrites — worker passes the
        full set it computed for the recipient)."""
        root = self.create_recipient_folder(recipient_id)
        np.save(root / "body" / "embeddings.npy",
                embeddings.astype(np.float32))

    def load_embeddings(self, recipient_id: str) -> np.ndarray:
        root = self.get_recipient_folder(recipient_id)
        f = root / "body" / "embeddings.npy" if root else None
        if f and f.exists():
            return np.load(f).astype(np.float32)
        return np.zeros((0, settings.reid_embedding_dim), dtype=np.float32)

    def load_gallery(self) -> tuple[np.ndarray, list[str]]:
        """Concatenate every recipient's embeddings for a FAISS rebuild."""
        all_emb: list[np.ndarray] = []
        ids: list[str] = []
        for root in sorted(self.base_path.glob("*")):
            if not root.is_dir():
                continue
            f = root / "body" / "embeddings.npy"
            if not f.exists():
                continue
            emb = np.load(f).astype(np.float32)
            if emb.ndim == 2 and emb.shape[0] > 0:
                all_emb.append(emb)
                ids.extend([root.name] * emb.shape[0])
        if not all_emb:
            return np.zeros((0, settings.reid_embedding_dim), dtype=np.float32), []
        return np.concatenate(all_emb, axis=0), ids

    # ---- status ------------------------------------------------------
    def set_status(self, recipient_id: str, **fields) -> None:
        root = self.create_recipient_folder(recipient_id)
        path = root / "status.json"
        cur = self.get_status(recipient_id)
        cur.update(fields)
        cur["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
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
