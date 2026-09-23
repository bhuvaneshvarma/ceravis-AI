from __future__ import annotations

import logging
import queue
import threading

import cv2
import numpy as np

from enrollment.enrollment_manager import EnrollmentManager
from common import clock
from common.crops import crop_person
from config.settings import settings
from detection.detection_schema import DetectionClass
from ingestion.illumination import Modality
from reid import crop_quality


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

    Night vision: every crop is embedded twice — as it is (the colour gallery,
    embeddings.npy, which the cloud keeps) and as luminance only (the infrared
    gallery, embeddings_ir.npy, device-local). Recipients enrolled before the
    infrared gallery existed are back-filled from their stored media on start.
    """

    VIDEO_SAMPLE_EVERY = 15          # ~ every 0.5 s at 30 fps
    MAX_FRAMES_PER_VIDEO = 40

    def __init__(self, manager: EnrollmentManager, gallery=None,
                 face_gallery=None) -> None:
        self._mgr = manager
        self._gallery = gallery       # shared FaissGallery (same one ReID queries)
        self._face_gallery = face_gallery   # shared FaceGallery (the second cue)
        self._face = None                   # FaceIdentity, loaded on first use
        # (job, recipient_id): "enroll" = the full pipeline, "ir" / "face" =
        # only build that gallery from already-stored media (back-fill).
        self._q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._detector = None
        self._extractor = None
        self._reid_error: str | None = None
        self._skipped: dict[str, int] = {}   # last job: unusable photos, by reason
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
        self._backfill_ir()
        self._backfill_faces()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def enqueue(self, recipient_id: str) -> None:
        self._mgr.set_status(recipient_id, state="queued",
                             message="waiting for embedding worker")
        self._q.put(("enroll", recipient_id))

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
                job, recipient_id = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if job == "face":
                try:
                    self._process_faces(recipient_id)
                except Exception:
                    # Faces are a second cue: a failure leaves body ReID intact.
                    logger.exception("enroll: face back-fill failed for %s",
                                     recipient_id)
                continue
            if job == "ir":
                try:
                    self._process_ir(recipient_id)
                except Exception:
                    # Night-gallery back-fill is an enhancement: a failure is
                    # logged and the recipient stays fully enrolled for day use.
                    logger.exception("enroll: IR gallery back-fill failed for %s",
                                     recipient_id)
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

        skipped = self._skipped_note()
        if not embeddings:
            self._mgr.set_status(recipient_id, state="error", photos=len(crops),
                                 embeddings=0,
                                 message="no usable photo of one person" + skipped)
            return

        arr = np.stack(embeddings, axis=0).astype(np.float32)
        self._mgr.save_embeddings(recipient_id, arr)
        self._mgr.save_embedding_labels(recipient_id, good_labels)
        self._save_ir(recipient_id, good_crops)
        faces = self._save_faces(recipient_id)
        # Keep a few small JPEG crops of the person for future reference.
        refs = self._mgr.save_reference_crops(recipient_id, good_crops)
        self._rebuild_gallery()
        self._upload_embeddings(recipient_id)
        self._mgr.set_status(recipient_id, state="ready", photos=len(crops),
                             embeddings=len(embeddings), references=refs,
                             faces=faces,
                             message=f"enrolled — {len(embeddings)} embeddings, "
                                     f"{faces} face(s), {refs} reference image(s)"
                                     + skipped)
        logger.info("enroll: %s ready (%d embeddings)", recipient_id, len(embeddings))

    def _skipped_note(self) -> str:
        """'; skipped 2 (no person), 1 (two people)' for the last collection."""
        if not self._skipped:
            return ""
        return "; skipped " + ", ".join(f"{n} ({why})"
                                        for why, n in self._skipped.items())

    # ---- infrared gallery -------------------------------------------
    def _save_ir(self, recipient_id: str, crops: list[np.ndarray]) -> int:
        """Embed the SAME crops as luminance only — the infrared gallery, row-
        aligned with the colour one (so the view labels line up). Returns the
        number of vectors saved."""
        ir = [self._extractor.embed(c, ir=True) for c in crops]
        ir = [e for e in ir if np.linalg.norm(e) > 0]
        if ir:
            self._mgr.save_embeddings(recipient_id,
                                      np.stack(ir, axis=0).astype(np.float32),
                                      Modality.IR.value)
        return len(ir)

    def _process_ir(self, recipient_id: str) -> None:
        """Back-fill: build the infrared gallery from stored media for a
        recipient enrolled before it existed. Leaves the colour gallery, the
        enrollment status and the cloud copy exactly as they are."""
        if not self._ensure_engines():
            return                          # retried on the next start
        crops, _labels = self._collect_crops(recipient_id)
        n = self._save_ir(recipient_id, crops)
        if n:
            self._rebuild_gallery()
        logger.info("enroll: %s infrared gallery built (%d vectors)",
                    recipient_id, n)

    def _backfill_ir(self) -> None:
        """Queue the infrared back-fill for every enrolled recipient that has
        a colour gallery and stored media but no infrared gallery yet."""
        try:
            roots = sorted(self._mgr.base_path.glob("*"))
        except Exception:
            logger.exception("enroll: could not scan for IR back-fill")
            return
        for root in roots:
            rid = root.name
            if not root.is_dir():
                continue
            if self._mgr.load_embeddings(rid).shape[0] == 0:
                continue                    # not enrolled (yet)
            if self._mgr.load_embeddings(rid, Modality.IR.value).shape[0] > 0:
                continue                    # already has one
            if not (self._mgr.media_names(rid) or self._mgr.list_videos(rid)):
                continue                    # nothing to build it from
            logger.info("enroll: queueing infrared gallery back-fill for %s", rid)
            self._q.put(("ir", rid))

    # ---- face gallery (the second identity cue) ----------------------
    def _save_faces(self, recipient_id: str) -> int:
        """Embed the one clear face in each stored photo (reid/face_identity).
        Faces are taken from the PHOTOS, not the body crops: a head-and-
        shoulders close-up is useless to body ReID but the best face there is.
        Returns how many faces were saved (0 when face identity is off)."""
        if self._face_gallery is None or not settings.face_enabled:
            return 0
        if self._face is None:
            from reid.face_identity import FaceIdentity
            self._face = FaceIdentity()
        if not self._face.ready:
            return 0
        vecs = []
        for p in self._mgr.list_photos(recipient_id):
            img = cv2.imread(str(p))
            if img is not None:
                v, _why = self._face.embed_photo(img)
                if v is not None:
                    vecs.append(v)
        from reid.face_identity import FACE_DIM
        arr = (np.stack(vecs).astype(np.float32) if vecs
               else np.zeros((0, FACE_DIM), dtype=np.float32))
        self._mgr.save_face_embeddings(recipient_id, arr)
        return len(vecs)

    def _process_faces(self, recipient_id: str) -> None:
        n = self._save_faces(recipient_id)
        if n:
            self._rebuild_gallery()
        logger.info("enroll: %s face gallery built (%d face(s))", recipient_id, n)

    def _backfill_faces(self) -> None:
        """Queue faces for every recipient enrolled before face identity
        existed. A recipient with no body embeddings (never enrolled, or
        switched off on purpose) is left alone."""
        if self._face_gallery is None or not settings.face_enabled:
            return
        for root in sorted(self._mgr.base_path.glob("*")):
            rid = root.name
            if (root.is_dir() and not self._mgr.has_face_embeddings(rid)
                    and self._mgr.load_embeddings(rid).shape[0] > 0
                    and self._mgr.media_names(rid)):
                logger.info("enroll: queueing face gallery back-fill for %s", rid)
                self._q.put(("face", rid))

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

        # Only a clear, usable view of ONE person may enter the gallery. The
        # gallery is the definition of who the recipient is; a photo that is
        # not unambiguously them teaches it someone — or something — else.
        crops: list[np.ndarray] = []
        kept: list[str] = []
        skipped: dict[str, int] = {}
        for img, label in zip(images, labels):
            crop, why = self._largest_person(img)
            if crop is None:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            crops.append(crop)
            kept.append(label)
        self._skipped = skipped
        if skipped:
            logger.info("enroll: %s — %d usable, skipped %s", recipient_id,
                        len(crops), skipped)
        return crops, kept

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
        """(crop, "") of the one person in `img`, or (None, why not).

        Cropped and gated EXACTLY like a live crop (same padding, same quality
        gate), so both sides of a match are prepared the same way. Two
        exceptions, both about deliberate enrollment framing: a box touching the
        frame edge is allowed (a posed full-body shot often does), and a second
        box that is merely a duplicate of the first (IoU >= 0.5) is ignored."""
        res = self._detector.detect(
            frame=img, camera_id="enroll", frame_id=0,
            timestamp=clock.now(),
        )
        people = [d for d in res.detections
                  if d.class_name == DetectionClass.PERSON]
        if not people:
            return None, "no person"
        people.sort(key=lambda d: d.bbox.area, reverse=True)
        best = people[0]
        b = best.bbox
        for other in people[1:]:
            o = other.bbox
            iw = max(0.0, min(b.x2, o.x2) - max(b.x1, o.x1))
            ih = max(0.0, min(b.y2, o.y2) - max(b.y1, o.y1))
            inter = iw * ih
            iou = inter / max(1e-6, b.area + o.area - inter)
            if iou < 0.5 and o.area >= 0.5 * b.area:
                return None, "two people"
        crop, _, _ = crop_person(img, b.x1, b.y1, b.x2, b.y2,
                                 settings.crop_padding_frac)
        h, w = img.shape[:2]
        q = crop_quality.assess(crop, b, w, h, best.confidence)
        if not q.ok and not q.truncated and "truncated" not in q.reason:
            return None, q.reason.split(" (")[0]
        return (crop, "") if crop.size else (None, "empty crop")

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
            # 'ready' is deliberately NOT here, even with no embeddings on disk:
            # removing a finished recipient's body/ folder is how an operator
            # switches the AI chain off (2026-09-23, several people in the room
            # and a wrong target raising false events). Rebuilding it from the
            # photos at start-up would silently switch the AI back on.
            # One exception, which is not a switch-off: embeddings that EXIST but
            # were made by a different ReID model (a model upgrade). They can
            # never match, so the recipient is re-embedded from their media.
            migrate = state == "ready" and self._mgr.stale_model(rid)
            if state not in self._RESUMABLE_STATES and not migrate:
                continue
            if self._mgr.load_embeddings(rid).shape[0] > 0:
                continue                       # already embedded (in gallery)
            if not (self._mgr.media_names(rid) or self._mgr.list_videos(rid)):
                continue                       # nothing to embed
            if migrate:
                logger.warning("enroll: %s was embedded by a different ReID "
                               "model — re-embedding from the stored media", rid)
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
        emb, ids, labels, mods = self._mgr.load_gallery()
        try:
            self._gallery.rebuild(emb, ids, labels, mods)
        except Exception:
            logger.exception("enroll: gallery rebuild failed")
        if self._face_gallery is not None:
            self._face_gallery.rebuild(self._mgr.load_face_gallery())
