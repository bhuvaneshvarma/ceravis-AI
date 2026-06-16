from __future__ import annotations

import logging
import threading
import time

from datetime import datetime, timezone

from common.crops import crop_person
from config.settings import settings
from enrollment.enrollment_manager import EnrollmentManager
from ingestion.frame_buffer import FrameBuffer
from reid.faiss_index import FaissGallery
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from reid.reid_extractor import ReIDExtractor
from reid.target_registry import TargetRegistry
from tracking.track_buffer import TrackBuffer


logger = logging.getLogger("reid")


class ReIDRunner:
    """
    Runs OSNet on tracked person crops at settings.reid_fps (default 2 FPS).

    Strategy:
      - Only crop already-tracked persons (no wasted compute).
      - One ID per (camera_id, track_id) cached; re-query only every N frames.
    """

    REQUERY_EVERY_N_FRAMES = 10

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        track_buffer: TrackBuffer,
        identity_buffer: IdentityBuffer,
        gallery: FaissGallery,
        target_registry: TargetRegistry | None = None,
        posture_buffer=None,
        enroll_manager: EnrollmentManager | None = None,
    ) -> None:
        self._frames = frame_buffer
        self._tracks = track_buffer
        self._identities = identity_buffer
        self._gallery = gallery
        self._targets = target_registry or TargetRegistry()
        self._postures = posture_buffer            # optional — labels adaptive captures
        self._enroll_mgr = enroll_manager or EnrollmentManager()
        self._extractor: ReIDExtractor | None = None

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_query_frame: dict[tuple[str, int], int] = {}
        self._last_adapt_rebuild = 0.0             # throttle adaptive gallery rebuilds

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        try:
            self._extractor = ReIDExtractor()
        except FileNotFoundError as exc:
            logger.warning("ReIDRunner disabled — ReID engine not built "
                           "(run scripts/export_reid.sh): %s", exc)
            return
        except Exception:
            logger.exception("ReIDRunner disabled — ReID engine failed to load")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="reid-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        interval = 1.0 / settings.reid_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("reid tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _tick(self) -> None:
        if self._extractor is None:
            return
        threshold = settings.reid_match_threshold

        pad = settings.crop_padding_frac
        for camera_id, track_result in self._tracks.get_all().items():
            frame_data = self._frames.get(camera_id)
            if frame_data is None or not track_result.tracks:
                continue

            target_tid = self._targets.get(camera_id)

            for track in track_result.tracks:
                key = (camera_id, track.track_id)

                # Focus: while the target lock is fresh, only re-verify the
                # target itself — skip embedding the other people (the GPU
                # saving you asked for). Non-target tracks are still counted
                # by detection/tracking, just not re-identified every tick.
                if target_tid is not None and track.track_id != target_tid:
                    continue

                last = self._last_query_frame.get(key, -1)
                if frame_data.frame_id - last < self.REQUERY_EVERY_N_FRAMES \
                   and self._identities.get(camera_id, track.track_id) is not None:
                    continue

                crop, _, _ = crop_person(
                    frame_data.frame, track.bbox.x1, track.bbox.y1,
                    track.bbox.x2, track.bbox.y2, pad,
                )
                if crop.size == 0:
                    continue

                emb = self._extractor.embed(crop)
                rid, score = self._gallery.search(emb)
                is_target = score >= threshold
                self._identities.update(
                    Identity(
                        track_id=track.track_id,
                        camera_id=camera_id,
                        frame_id=frame_data.frame_id,
                        timestamp=frame_data.timestamp,
                        recipient_id=rid if is_target else None,
                        is_target=is_target,
                        confidence=float(score),
                    )
                )
                if is_target and rid is not None:
                    # Lock (or refresh) the target on this track id.
                    self._targets.lock(camera_id, track.track_id, rid)
                    # Online learning: capture this appearance if confident.
                    if (settings.reid_adaptive_enabled
                            and score >= settings.reid_adaptive_min_score):
                        self._maybe_adapt(camera_id, track.track_id, rid, emb)
                self._last_query_frame[key] = frame_data.frame_id

    def _maybe_adapt(self, camera_id: str, track_id: int, rid: str, emb) -> None:
        """
        Persist a high-confidence target embedding into the recipient's adaptive
        store (vectors only) and, when enough time has passed, rebuild the shared
        gallery so the new appearance starts helping matches. Best-effort: any
        failure here must never disturb live identification.
        """
        try:
            label = ""
            if self._postures is not None:
                rec = self._postures.get(camera_id, track_id)
                label = rec.posture.value if rec is not None else ""
            added = self._enroll_mgr.append_adaptive(
                rid, emb, label,
                cap=settings.reid_adaptive_max,
                dedup_cos=settings.reid_adaptive_dedup_cos,
            )
            if not added:
                return
            now = time.monotonic()
            if now - self._last_adapt_rebuild >= settings.reid_adaptive_rebuild_secs:
                emb_all, ids = self._enroll_mgr.load_gallery()
                self._gallery.rebuild(emb_all, ids)
                self._last_adapt_rebuild = now
                logger.info("reid: adaptive store updated for %s "
                            "(gallery now %d vectors)", rid, len(ids))
        except Exception:
            logger.exception("reid: adaptive capture failed")
