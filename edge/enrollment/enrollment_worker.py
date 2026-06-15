from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone

import cv2
import numpy as np

from config.settings import settings
from enrollment.enrollment_manager import EnrollmentManager


logger = logging.getLogger("enrollment")


class EnrollmentWorker:
    """
    Background embedding worker — the production enrollment pipeline:

        enqueue(recipient_id)              (API, after media is stored)
              -> queue
              -> _process(): detect person crop -> FastReid embed
              -> save embeddings -> rebuild FAISS gallery

    Engines are loaded lazily and independently of the live pipeline:
      - detection engine crops the body from each enrollment photo
      - FastReid engine turns crops into embeddings
    If the FastReid engine isn't built yet (ReID not enabled), media is
    still stored and the job is marked 'pending_reid' so it can be
    re-run later — nothing is lost.

    Video frames are sampled; live captures arrive as already-saved photos.
    """

    VIDEO_SAMPLE_EVERY = 15          # ~ every 0.5 s at 30 fps
    MAX_FRAMES_PER_VIDEO = 40

    def __init__(self, manager: EnrollmentManager, gallery=None) -> None:
        self._mgr = manager
        self._gallery = gallery       # shared FaissGallery (same one ReID queries)
        self._q: "queue.Queue[str]" = queue.Queue()
        self._detector = None
        self._extractor = None
        self._engines_tried = False
        self._running = False
        self._thread: threading.Thread | None = None

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="enroll-worker",
        )
        self._thread.start()
        # Load any embeddings produced in a previous run into the gallery.
        self._rebuild_gallery()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def enqueue(self, recipient_id: str) -> None:
        self._mgr.set_status(recipient_id, state="queued",
                             message="waiting for embedding worker")
        self._q.put(recipient_id)

    # ---- engines (lazy) ---------------------------------------------
    def _ensure_engines(self) -> bool:
        if self._engines_tried:
            return self._detector is not None and self._extractor is not None
        self._engines_tried = True
        try:
            from detection.yolo_detector import YOLODetector
            self._detector = YOLODetector()
        except Exception:
            logger.exception("enroll: detection engine unavailable")
        try:
            from reid.reid_extractor import ReIDExtractor
            self._extractor = ReIDExtractor()
        except Exception:
            logger.warning("enroll: ReID engine unavailable — embeddings "
                           "deferred (run scripts/export_reid.sh)")
        return self._detector is not None and self._extractor is not None

    # ---- main loop ---------------------------------------------------
    def _run(self) -> None:
        while self._running:
            try:
                recipient_id = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process(recipient_id)
            except Exception:
                logger.exception("enroll: job failed for %s", recipient_id)
                self._mgr.set_status(recipient_id, state="error",
                                     message="see logs")

    def _process(self, recipient_id: str) -> None:
        self._mgr.set_status(recipient_id, state="processing")

        crops = self._collect_crops(recipient_id)
        if not self._ensure_engines():
            reason = ("media stored; run scripts/export_reid.sh to build the "
                      "ReID engine, then re-enroll to generate embeddings")
            self._mgr.set_status(recipient_id, state="pending_reid",
                                 photos=len(crops), embeddings=0, message=reason)
            logger.info("enroll: %s deferred — %s", recipient_id, reason)
            return

        embeddings, good_crops = [], []
        for crop in crops:
            emb = self._extractor.embed(crop)
            if np.linalg.norm(emb) > 0:
                embeddings.append(emb)
                good_crops.append(crop)

        if not embeddings:
            self._mgr.set_status(recipient_id, state="error", photos=len(crops),
                                 embeddings=0, message="no person found in media")
            return

        arr = np.stack(embeddings, axis=0).astype(np.float32)
        self._mgr.save_embeddings(recipient_id, arr)
        # Keep a few small JPEG crops of the person for future reference.
        refs = self._mgr.save_reference_crops(recipient_id, good_crops)
        self._rebuild_gallery()
        self._mgr.set_status(recipient_id, state="ready", photos=len(crops),
                             embeddings=len(embeddings), references=refs,
                             message=f"enrolled — {len(embeddings)} embeddings, "
                                     f"{refs} reference image(s)")
        logger.info("enroll: %s ready (%d embeddings)", recipient_id, len(embeddings))

    # ---- crop extraction --------------------------------------------
    def _collect_crops(self, recipient_id: str) -> list[np.ndarray]:
        """Largest-person crop from every photo + sampled video frame."""
        images: list[np.ndarray] = []
        for p in self._mgr.list_photos(recipient_id):
            img = cv2.imread(str(p))
            if img is not None:
                images.append(img)
        for v in self._mgr.list_videos(recipient_id):
            images.extend(self._sample_video(str(v)))

        if self._detector is None:
            # No detector: use whole images as crops (FastReid still resizes).
            return images

        crops: list[np.ndarray] = []
        for img in images:
            crop = self._largest_person(img)
            crops.append(crop if crop is not None else img)
        return crops

    def _sample_video(self, path: str) -> list[np.ndarray]:
        frames: list[np.ndarray] = []
        cap = cv2.VideoCapture(path)
        i = 0
        while len(frames) < self.MAX_FRAMES_PER_VIDEO:
            ok, frame = cap.read()
            if not ok:
                break
            if i % self.VIDEO_SAMPLE_EVERY == 0:
                frames.append(frame)
            i += 1
        cap.release()
        return frames

    def _largest_person(self, img: np.ndarray):
        res = self._detector.detect(
            frame=img, camera_id="enroll", frame_id=0,
            timestamp=datetime.now(timezone.utc),
        )
        if not res.detections:
            return None
        best = max(res.detections, key=lambda d: d.bbox.area)
        x1, y1 = max(0, int(best.bbox.x1)), max(0, int(best.bbox.y1))
        x2, y2 = int(best.bbox.x2), int(best.bbox.y2)
        crop = img[y1:y2, x1:x2]
        return crop if crop.size else None

    # ---- gallery -----------------------------------------------------
    def _rebuild_gallery(self) -> None:
        if self._gallery is None:
            return
        emb, ids = self._mgr.load_gallery()
        try:
            self._gallery.rebuild(emb, ids)
        except Exception:
            logger.exception("enroll: gallery rebuild failed")
