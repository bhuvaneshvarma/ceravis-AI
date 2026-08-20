from __future__ import annotations

import logging
import queue
import threading
import time

from config.settings import settings
from enrollment.enrollment_manager import EnrollmentManager
from reid.faiss_index import FaissGallery
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from reid.recency_buffer import RecencyBuffer
from reid.target_lock import TargetLockManager
from reid.target_registry import TargetRegistry
from tracking.track_buffer import TrackBuffer
from tracking.track_feature_buffer import TrackFeatureBuffer


logger = logging.getLogger("reid")


class ReIDRunner:
    """
    Identity + target-lock orchestrator.

    The heavy lifting (OSNet appearance) now happens once, inside the tracker,
    and lands in the TrackFeatureBuffer. This runner is light: at reid_fps it
    reads per-track features, runs the hybrid set-to-set gallery match through
    the TargetLockManager (acquire / verify / freeze-on-occlusion / release /
    reacquire), writes identities + the shared target lock, and hands confident,
    NON-occluded appearances to a background thread for adaptive learning.

    No frame access, no inference on this path — so it can't lag the stream.
    """

    def __init__(
        self,
        track_buffer: TrackBuffer,
        feature_buffer: TrackFeatureBuffer,
        identity_buffer: IdentityBuffer,
        gallery: FaissGallery,
        target_registry: TargetRegistry | None = None,
        posture_buffer=None,
        enroll_manager: EnrollmentManager | None = None,
    ) -> None:
        self._tracks = track_buffer
        self._features = feature_buffer
        self._identities = identity_buffer
        self._gallery = gallery
        self._targets = target_registry or TargetRegistry()
        self._postures = posture_buffer
        self._enroll_mgr = enroll_manager or EnrollmentManager()
        # Short-term memory of how the target looks RIGHT NOW. Feeds the
        # acquire/reacquire fusion so a look-alike can't take the ID while a
        # live recent window exists. See reid/recency_buffer.py.
        self._recency = RecencyBuffer()
        self._manager = TargetLockManager(gallery, recency=self._recency)

        self._running = False
        self._thread: threading.Thread | None = None

        # Adaptive online-learning on a separate low-rate thread so its disk I/O
        # never blocks identity decisions. The tick only does a cheap, throttled
        # hand-off onto this queue.
        self._adapt_q: "queue.Queue[tuple[str, object, str]]" = queue.Queue(maxsize=64)
        self._adapt_thread: threading.Thread | None = None
        self._last_adapt_attempt = 0.0
        self._last_adapt_rebuild = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="reid-runner")
        self._thread.start()
        if settings.reid_adaptive_enabled:
            self._adapt_thread = threading.Thread(
                target=self._adaptive_loop, daemon=True, name="reid-adaptive")
            self._adapt_thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
        if self._adapt_thread:
            self._adapt_thread.join(timeout)

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
        for camera_id, track_result in self._tracks.get_all().items():
            if not track_result.tracks:
                continue
            boxes = {t.track_id: (t.bbox.x1, t.bbox.y1, t.bbox.x2, t.bbox.y2)
                     for t in track_result.tracks}

            def feat_for(tid: int):
                rec = self._features.get(camera_id, tid)
                return rec.smooth if rec is not None else None

            outcome = self._manager.update(camera_id, boxes, feat_for)

            # Apply the lock decision to the shared registry (pose + UI read it).
            if outcome.released:
                self._targets.unlock(camera_id)
            if outcome.target_track_id is not None and outcome.recipient_id:
                self._targets.lock(camera_id, outcome.target_track_id,
                                   outcome.recipient_id)

            # Publish identities.
            ts = track_result.timestamp
            fid = track_result.frame_id
            for tid, (rid, is_target, score, view) in outcome.identities.items():
                self._identities.update(Identity(
                    track_id=tid, camera_id=camera_id, frame_id=fid, timestamp=ts,
                    recipient_id=rid if is_target else None,
                    is_target=is_target, confidence=float(score),
                    view_label=view if is_target else None,
                    recency_score=(outcome.recency if is_target else None)))

            # Adaptive capture — ONLY for a confidently-matched, NON-occluded
            # target (the manager already gated occlusion), so we never learn an
            # intruder's appearance into the recipient's gallery.
            # outcome.adaptive is set exactly on a CONFIRMED, NON-OCCLUDED
            # sighting — the only look we are willing to remember as "this is
            # the target right now". Recency is pushed on every one of them;
            # the adaptive store keeps its own slower throttle underneath.
            if outcome.adaptive is not None:
                tid, rid, score = outcome.adaptive
                rec = self._features.get(camera_id, tid)
                if rec is not None:
                    if score >= settings.reid_recency_min_push_score:
                        self._recency.push(rid, rec.curr)
                    self._queue_adapt(camera_id, tid, rid, score, rec.curr)

    # ---- adaptive online learning (off the inference tick) -----------
    def _queue_adapt(self, camera_id, track_id, rid, score, emb) -> None:
        if not settings.reid_adaptive_enabled or score < settings.reid_adaptive_min_score:
            return
        now = time.monotonic()
        if now - self._last_adapt_attempt < settings.reid_adaptive_min_interval_secs:
            return
        self._last_adapt_attempt = now
        label = ""
        if self._postures is not None:
            rec = self._postures.get(camera_id, track_id)
            label = rec.posture.value if rec is not None else ""
        try:
            self._adapt_q.put_nowait((rid, emb.copy(), label))
        except queue.Full:
            pass

    def _adaptive_loop(self) -> None:
        while self._running:
            try:
                rid, emb, label = self._adapt_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                added = self._enroll_mgr.append_adaptive(
                    rid, emb, label,
                    cap=settings.reid_adaptive_max,
                    dedup_cos=settings.reid_adaptive_dedup_cos)
                if not added:
                    continue
                logger.info("reid: adaptive sample saved for %s (label=%s)",
                            rid, label or "—")
                now = time.monotonic()
                if now - self._last_adapt_rebuild >= settings.reid_adaptive_rebuild_secs:
                    emb_all, ids, labels = self._enroll_mgr.load_gallery()
                    self._gallery.rebuild(emb_all, ids, labels)
                    self._last_adapt_rebuild = now
                    logger.info("reid: adaptive store updated for %s "
                                "(gallery now %d vectors)", rid, len(ids))
            except Exception:
                logger.exception("reid: adaptive capture failed")
