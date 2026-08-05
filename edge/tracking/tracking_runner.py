from __future__ import annotations

import logging
import threading
import time

import numpy as np

from common.crops import crop_person
from config.settings import settings
from detection.detection_buffer import DetectionBuffer
from detection.detection_schema import BoundingBox, DetectionClass
from ingestion.frame_buffer import FrameBuffer
from tracking.botsort import BoTSORT
from tracking.track_buffer import TrackBuffer
from tracking.track_feature_buffer import TrackFeatureBuffer
from tracking.track_schema import Track, TrackResult


logger = logging.getLogger("tracking")


class TrackingRunner:
    """
    Per-camera clean-room BoT-SORT, polled from the DetectionBuffer.

    Appearance (OSNet, the same engine the gallery uses) is fused into the
    association so IDs survive a crossover. To stay cheap, embeddings are only
    computed when there are several people in frame (the case that needs it);
    with a single person the tracker is pure motion + a low-rate feature refresh
    so the gallery match still has something fresh to compare.
    """

    def __init__(
        self,
        detection_buffer: DetectionBuffer,
        track_buffer: TrackBuffer,
        frame_buffer: FrameBuffer | None = None,
        feature_buffer: TrackFeatureBuffer | None = None,
        metrics_registry=None,
        gallery=None,
        target_registry=None,
    ) -> None:
        self._detections = detection_buffer
        self._tracks = track_buffer
        self._frames = frame_buffer
        self._features = feature_buffer
        # Active-camera focus: once the recipient is locked on a camera, run the
        # GPU-heavy work (BoT-SORT + OSNet + pose + ReID all flow from here) ONLY
        # on that camera; the others idle until the lock lapses. Detection stays
        # all-cameras so recording keeps covering the whole home. Empty set =
        # searching (process every camera to (re)find the target).
        self._targets = target_registry
        # Enrollment gate: tracking/ReID/pose/rules run ONLY when the gallery holds
        # at least one enrolled target embedding. Until then the pipeline stops at
        # YOLO detection (which still drives recording) — no wasted appearance /
        # tracking / pose / rule work on a device with nobody enrolled. Re-enables
        # itself the instant enrollment lands (the gallery is rebuilt live).
        self._gallery = gallery
        self._enrolled = False
        self._gate_logged = 0.0
        self._metrics = (
            metrics_registry.get_or_create("reid_embed") if metrics_registry else None
        )

        self._trackers: dict[str, BoTSORT] = {}
        self._last_seen_frame: dict[str, int] = {}
        self._last_embed: dict[str, float] = {}

        self._extractor = None              # OSNet; None => motion-only tracking
        self._with_reid = False

        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        # Appearance is optional: if the ReID engine isn't built (e.g. on a dev
        # box) the tracker degrades to motion-only ByteTrack and the rest of the
        # pipeline keeps working.
        if (settings.tracker_with_reid and self._frames is not None
                and self._features is not None):
            try:
                from reid.reid_extractor import ReIDExtractor
                self._extractor = ReIDExtractor()
                self._with_reid = True
                logger.info("Tracking: BoT-SORT with OSNet appearance fusion")
            except FileNotFoundError as exc:
                logger.warning("Tracking: appearance off — ReID engine not built "
                               "(%s); running motion-only BoT-SORT", exc)
            except Exception:
                logger.exception("Tracking: appearance off — ReID engine load failed")
        else:
            logger.info("Tracking: motion-only BoT-SORT (appearance disabled)")

        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="tracking-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---- internals ---------------------------------------------------
    def _tracker(self, camera_id: str) -> BoTSORT:
        if camera_id not in self._trackers:
            self._trackers[camera_id] = BoTSORT(
                track_high_thresh=settings.tracker_high_thresh,
                track_low_thresh=settings.tracker_low_thresh,
                new_track_thresh=settings.tracker_new_track_thresh,
                match_thresh=settings.tracker_match_thresh,
                track_buffer=settings.tracker_track_buffer,
                proximity_thresh=settings.tracker_proximity_thresh,
                appearance_thresh=settings.tracker_appearance_thresh,
                with_reid=self._with_reid,
                frame_rate=settings.detection_fps,
            )
        return self._trackers[camera_id]

    def _run(self) -> None:
        interval = 1.0 / settings.detection_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("tracking tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _reid_ready(self) -> bool:
        """Whether any target is enrolled — the gate for the whole AI chain below
        detection. size() is the gallery's live embedding count, so this flips on
        the moment enrollment finishes and off if the gallery is emptied."""
        ready = self._gallery is not None and self._gallery.size() > 0
        if ready and not self._enrolled:
            logger.info("Tracking ENABLED — %d enrolled embedding(s) present",
                        self._gallery.size())
            self._enrolled = True
        elif not ready and (self._enrolled or self._gate_logged == 0.0
                            or time.monotonic() - self._gate_logged > 60):
            logger.info("Tracking IDLE — no enrolled embeddings; running "
                        "detection-only (recording stays active). Enroll the "
                        "recipient to start tracking / ReID / pose / alerts.")
            self._gate_logged = time.monotonic()
            self._enrolled = False
        return ready

    def _active_cameras(self) -> set:
        """Cameras the GPU-heavy chain runs on. Once the recipient is locked on a
        camera, focus ONLY there (this is where the old detection-level focus now
        lives); with no lock, empty set = scan EVERY camera to (re)find them."""
        if not settings.active_camera_only or self._targets is None:
            return set()
        try:
            return set(self._targets.all().keys())
        except Exception:
            return set()

    def _tick(self) -> None:
        if not self._reid_ready():
            return                              # gated: stop at YOLO detection
        active = self._active_cameras()         # empty => searching (all cameras)
        for camera_id, det_result in self._detections.get_all().items():
            if active and camera_id not in active:
                continue                        # recipient is elsewhere; skip (recording still runs)
            if self._last_seen_frame.get(camera_id) == det_result.frame_id:
                continue
            self._last_seen_frame[camera_id] = det_result.frame_id

            persons = [d for d in det_result.detections
                       if d.class_name == DetectionClass.PERSON]
            if not persons:
                self._tracks.update(TrackResult(
                    camera_id=camera_id, frame_id=det_result.frame_id,
                    timestamp=det_result.timestamp, tracks=[]))
                if self._features is not None:
                    self._features.prune(camera_id, set())
                continue

            dets_xywh = np.array(
                [[(d.bbox.x1 + d.bbox.x2) / 2.0, (d.bbox.y1 + d.bbox.y2) / 2.0,
                  d.bbox.width, d.bbox.height] for d in persons], dtype=np.float32)
            scores = np.array([d.confidence for d in persons], dtype=np.float32)

            feats = self._maybe_embed(camera_id, persons)
            active = self._tracker(camera_id).update(dets_xywh, scores, feats)

            out: list[Track] = []
            alive: set[int] = set()
            for st in active:
                x1, y1, x2, y2 = st.xyxy
                out.append(Track(
                    track_id=int(st.track_id), camera_id=camera_id,
                    frame_id=det_result.frame_id, timestamp=det_result.timestamp,
                    bbox=BoundingBox(x1=float(x1), y1=float(y1),
                                     x2=float(x2), y2=float(y2)),
                    confidence=float(st.score)))
                alive.add(int(st.track_id))
                if self._features is not None and st.smooth_feat is not None:
                    self._features.update(
                        camera_id, int(st.track_id),
                        smooth=st.smooth_feat.copy(),
                        curr=(st.curr_feat.copy() if st.curr_feat is not None
                              else st.smooth_feat.copy()),
                        frame_id=det_result.frame_id, timestamp=det_result.timestamp)

            self._tracks.update(TrackResult(
                camera_id=camera_id, frame_id=det_result.frame_id,
                timestamp=det_result.timestamp, tracks=out))
            if self._features is not None:
                self._features.prune(camera_id, alive)

    def _maybe_embed(self, camera_id: str, persons) -> np.ndarray | None:
        """OSNet embeddings for each person, gated to keep the common case cheap."""
        if not self._with_reid or self._extractor is None or self._frames is None:
            return None
        now = time.monotonic()
        many = len(persons) >= settings.tracker_appearance_min_persons
        due = (now - self._last_embed.get(camera_id, 0.0)) >= (1.0 / settings.reid_fps)
        if not (many or due):
            return None
        fd = self._frames.get(camera_id)
        if fd is None:
            return None

        t = time.perf_counter()
        out = []
        for d in persons:
            crop, _, _ = crop_person(fd.frame, d.bbox.x1, d.bbox.y1,
                                     d.bbox.x2, d.bbox.y2, settings.crop_padding_frac)
            if crop.size == 0:
                out.append(np.zeros(settings.reid_embedding_dim, dtype=np.float32))
            else:
                out.append(self._extractor.embed(crop))
        if self._metrics:
            self._metrics.record(time.perf_counter() - t)
        self._last_embed[camera_id] = now
        return np.stack(out, axis=0)
