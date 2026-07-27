from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone

import cv2
import numpy as np

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
        self._reid_error: str | None = None
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
        # Load any embeddings produced in a previous run into the gallery,
        # then re-queue recipients that were committed but never embedded
        # (e.g. enrolled before the ReID engine was built) so a build +
        # restart finishes them automatically — no manual re-enroll needed.
        self._rebuild_gallery()
        self._resume_pending()

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
        """
        Load engines lazily, retrying each one on every job until it succeeds
        — so building an engine and re-queuing (even without restarting) is
        enough to recover. Once loaded an engine is cached and never rebuilt.

        The ReID extractor is the hard requirement (it produces the
        embeddings); the detector is best-effort and only tightens crops.
        Returns True when ReID is ready.
        """
        if self._detector is None:
            try:
                from detection.yolo_detector import YOLODetector
                self._detector = YOLODetector()
            except Exception:
                logger.warning("enroll: detector unavailable — enrolling on "
                               "whole images (crops not tightened)")
        if self._extractor is None:
            try:
                from reid.reid_extractor import ReIDExtractor
                self._extractor = ReIDExtractor()
                self._reid_error = None
            except FileNotFoundError:
                self._reid_error = ("ReID engine not built — run "
                                    "setup/export_reid.sh, then re-enroll")
                logger.warning("enroll: %s", self._reid_error)
            except Exception as exc:
                # Engine file exists but failed to load (corrupt, TRT version
                # mismatch, dim mismatch, …). Surface the REAL reason with a
                # full traceback instead of misdirecting to export_reid.sh.
                self._reid_error = f"ReID engine failed to load: {exc}"
                logger.exception("enroll: ReID engine load failed")
        return self._extractor is not None

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

        # Load engines first so the detector (if available) tightens crops.
        reid_ready = self._ensure_engines()
        crops, labels = self._collect_crops(recipient_id)
        if not reid_ready:
            reason = self._reid_error or (
                "media stored; run setup/export_reid.sh to build the ReID "
                "engine, then re-enroll to generate embeddings")
            self._mgr.set_status(recipient_id, state="pending_reid",
                                 photos=len(crops), embeddings=0, message=reason)
            logger.info("enroll: %s deferred — %s", recipient_id, reason)
            return

        embeddings, good_crops, good_labels = [], [], []
        for crop, label in zip(crops, labels):
            emb = self._extractor.embed(crop)
            if np.linalg.norm(emb) > 0:
                embeddings.append(emb)
                good_crops.append(crop)
                good_labels.append(label)

        if not embeddings:
            self._mgr.set_status(recipient_id, state="error", photos=len(crops),
                                 embeddings=0, message="no person found in media")
            return

        arr = np.stack(embeddings, axis=0).astype(np.float32)
        self._mgr.save_embeddings(recipient_id, arr)
        self._mgr.save_embedding_labels(recipient_id, good_labels)
        # Keep a few small JPEG crops of the person for future reference.
        refs = self._mgr.save_reference_crops(recipient_id, good_crops)
        self._rebuild_gallery()
        self._upload_embeddings(recipient_id)
        self._mgr.set_status(recipient_id, state="ready", photos=len(crops),
                             embeddings=len(embeddings), references=refs,
                             message=f"enrolled — {len(embeddings)} embeddings, "
                                     f"{refs} reference image(s)")
        logger.info("enroll: %s ready (%d embeddings)", recipient_id, len(embeddings))

    # ---- crop extraction --------------------------------------------
    def _collect_crops(self, recipient_id: str):
        """Largest-person crop + its label from every photo / sampled video
        frame. Returns (crops, labels) aligned 1:1; photo labels come from the
        capture sidecar, sampled video frames are unlabeled."""
        labels_map = self._mgr.get_labels(recipient_id)
        images: list[np.ndarray] = []
        labels: list[str] = []
        for p in self._mgr.list_photos(recipient_id):
            img = cv2.imread(str(p))
            if img is not None:
                images.append(img)
                labels.append(labels_map.get(p.name, ""))
        for v in self._mgr.list_videos(recipient_id):
            for frame in self._sample_video(str(v)):
                images.append(frame)
                labels.append("")

        if self._detector is None:
            # No detector: use whole images as crops (the extractor resizes).
            return images, labels

        crops: list[np.ndarray] = []
        for img in images:
            crop = self._largest_person(img)
            crops.append(crop if crop is not None else img)
        return crops, labels

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

    # ---- resume ------------------------------------------------------
    # States that mean "committed for embedding but not finished" — these are
    # safe to auto-resume. 'review'/'none' (media added, not yet committed)
    # and 'ready' (already embedded) are intentionally excluded.
    _RESUMABLE_STATES = {"queued", "processing", "pending_reid", "error"}

    def _resume_pending(self) -> None:
        """Re-queue committed recipients that still have no embeddings, so a
        ReID engine built after they were enrolled finishes them on the next
        start instead of leaving them stuck in 'pending_reid'."""
        try:
            roots = sorted(self._mgr.base_path.glob("*"))
        except Exception:
            logger.exception("enroll: could not scan for pending recipients")
            return
        resumed = 0
        for root in roots:
            if not root.is_dir():
                continue
            rid = root.name
            state = self._mgr.get_status(rid).get("state")
            if state not in self._RESUMABLE_STATES:
                continue
            if self._mgr.load_embeddings(rid).shape[0] > 0:
                continue                       # already embedded (in gallery)
            if not (self._mgr.media_names(rid) or self._mgr.list_videos(rid)):
                continue                       # nothing to embed
            logger.info("enroll: resuming %s (was '%s')", rid, state)
            self.enqueue(rid)
            resumed += 1
        if resumed:
            logger.info("enroll: re-queued %d pending recipient(s)", resumed)

    # ---- cloud sync --------------------------------------------------
    def _upload_embeddings(self, recipient_id: str) -> None:
        """Best-effort: PUT this recipient's embeddings .npy to the app server
        (uploadEmbeddingFile, fileCategory EMBEDDING) so the ReID vectors follow
        the patient's account and survive a device reflash. Runs on the worker
        thread, so it never blocks or fails enrollment — a transport error is
        logged and dropped. The whole (K×dim) matrix is sent, not a summary."""
        try:
            from configuration.account_config import patient_user_id
            from integration.ceravis_api import (is_configured,
                                                 upload_embedding_file)
            if not is_configured():
                return
            pid = patient_user_id()               # = ceravisUserId (the account)
            f = self._mgr.base_path / recipient_id / "body" / "embeddings.npy"
            if not pid or not f.exists():
                return
            upload_embedding_file("EMBEDDING", pid, f"{pid}.npy", f.read_bytes())
        except Exception:
            logger.warning("enroll: embedding upload failed for %s", recipient_id)

    # ---- gallery -----------------------------------------------------
    def _rebuild_gallery(self) -> None:
        if self._gallery is None:
            return
        emb, ids, labels = self._mgr.load_gallery()
        try:
            self._gallery.rebuild(emb, ids, labels)
        except Exception:
            logger.exception("enroll: gallery rebuild failed")
