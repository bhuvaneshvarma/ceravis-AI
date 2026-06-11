from __future__ import annotations

from pathlib import Path

from config.settings import settings


class EnrollmentManager:
    """
    Manages on-disk per-recipient enrollment folders.

    Layout:
        data/recipients/<recipient_id>/
            photos/      raw upload photos
            videos/      enrollment videos
            face/        face crops + embeddings
            body/        body crops + ReID embeddings
    """

    SUBDIRS = ("photos", "videos", "face", "body")

    def __init__(self) -> None:
        self.base_path = settings.data_path / "recipients"
        self.base_path.mkdir(parents=True, exist_ok=True)

    def create_recipient_folder(self, recipient_id: str) -> Path:
        root = self.base_path / recipient_id
        root.mkdir(exist_ok=True)
        for sub in self.SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
        return root

    def get_recipient_folder(self, recipient_id: str) -> Path | None:
        path = self.base_path / recipient_id
        return path if path.exists() else None
