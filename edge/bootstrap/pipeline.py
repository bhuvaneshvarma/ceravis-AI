from __future__ import annotations

"""
Edge processing pipeline assembly — builds, starts and stops every runner, and
exposes the shared buffers/services for the API layer.

Kept out of main.py so that file stays a thin FastAPI wiring shell. Behaviour is
identical to the original inline lifespan: each stage is optional (guarded), and
the pipeline degrades gracefully when a component (engine/gallery) is missing.
"""

import logging
import socket

from config.settings import settings

from ingestion.camera_manager import CameraManager
from detection.detection_buffer import DetectionBuffer
from detection.detection_runner import DetectionRunner
from tracking.track_buffer import TrackBuffer
from tracking.track_feature_buffer import TrackFeatureBuffer
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer
from reid.identity_buffer import IdentityBuffer
from reid.target_registry import TargetRegistry
from reid.faiss_index import FaissGallery
from events.event_bus import EventBus
from storage.sqlite_store import SqliteStore
from storage.event_store import EventStore
from events.event_writer import EventWriter
from monitoring.pipeline_metrics import MetricsRegistry
from monitoring.system_monitor import SystemMonitor


logger = logging.getLogger("ceravis")


def lan_urls(port: int = 8000) -> list[str]:
    """Best-effort LAN addresses the API is reachable on, for logging."""
    urls = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no traffic sent; just resolves the route
        urls.append(f"http://{s.getsockname()[0]}:{port}")
        s.close()
    except Exception:
        pass
    try:
        urls.append(f"http://{socket.gethostname()}.local:{port}")
    except Exception:
        pass
    return urls


class Pipeline:
    """Owns the whole runtime graph. `start(loop)` builds + starts everything,
    `attach(state)` publishes the shared objects to app.state, `stop()` tears it
    down in reverse dependency order."""

    def __init__(self) -> None:
        self._state: dict = {}          # exposed on app.state
        self._shutdown: list = []       # runners stopped (in order) on shutdown
        self._camera_manager = None
        self._system_monitor = None
        self._sqlite_store = None

    # ---- lifecycle ---------------------------------------------------
    def start(self, loop) -> None:
        # ---- ingestion ---------------------------------------------
        camera_manager = CameraManager()
        camera_manager.start_all()
        self._camera_manager = camera_manager
        frames = camera_manager.frame_buffer

        # ---- buffers (always on) -----------------------------------
        detection_buffer = DetectionBuffer()
        track_buffer = TrackBuffer()
        feature_buffer = TrackFeatureBuffer()      # per-track OSNet features (tracker -> reid)
        pose_buffer = PoseBuffer()
        posture_buffer = PostureBuffer()
        identity_buffer = IdentityBuffer()
        target_registry = TargetRegistry()         # shared "who is the target" lock

        # ---- monitoring --------------------------------------------
        metrics_registry = MetricsRegistry()
        system_monitor = SystemMonitor()
        system_monitor.start()
        self._system_monitor = system_monitor

        # ---- detection (focus on the recipient's camera) -----------
        detection_runner = DetectionRunner(
            frame_buffer=frames, detection_buffer=detection_buffer,
            metrics_registry=metrics_registry, target_registry=target_registry)
        try:
            detection_runner.start()
        except Exception:
            logger.exception("DetectionRunner failed to start")

        # ---- tracking (clean-room BoT-SORT + OSNet appearance) -----
        tracking_runner = None
        try:
            from tracking.tracking_runner import TrackingRunner
            tracking_runner = TrackingRunner(
                detection_buffer, track_buffer, frame_buffer=frames,
                feature_buffer=feature_buffer, metrics_registry=metrics_registry)
            tracking_runner.start()
        except Exception:
            logger.exception("TrackingRunner disabled")

        # ---- pose + posture ----------------------------------------
        pose_runner = None
        posture_tracker = None
        try:
            from pose.pose_runner import PoseRunner
            pose_runner = PoseRunner(
                frame_buffer=frames, pose_buffer=pose_buffer,
                track_buffer=track_buffer, posture_buffer=posture_buffer,
                metrics_registry=metrics_registry, target_registry=target_registry)
            pose_runner.start()
            posture_tracker = pose_runner.posture_tracker
        except Exception:
            logger.exception("PoseRunner disabled")

        # ---- reid gallery (shared by ReID runner + enrollment) -----
        gallery: FaissGallery | None = None
        try:
            gallery = FaissGallery()
        except Exception:
            logger.exception("FAISS gallery unavailable (ReID + enrollment off)")

        # ---- reid --------------------------------------------------
        reid_runner = None
        if gallery is not None:
            try:
                from reid.reid_runner import ReIDRunner
                reid_runner = ReIDRunner(
                    track_buffer=track_buffer, feature_buffer=feature_buffer,
                    identity_buffer=identity_buffer, gallery=gallery,
                    target_registry=target_registry, posture_buffer=posture_buffer)
                reid_runner.start()
            except Exception:
                logger.exception("ReIDRunner disabled")

        # ---- enrollment worker -------------------------------------
        enroll_worker = None
        try:
            from enrollment.enrollment_manager import EnrollmentManager
            from enrollment.enrollment_worker import EnrollmentWorker
            enroll_worker = EnrollmentWorker(EnrollmentManager(), gallery=gallery)
            enroll_worker.start()
        except Exception:
            logger.exception("EnrollmentWorker disabled")

        # ---- events + storage --------------------------------------
        event_bus = EventBus()
        sqlite_store = SqliteStore(settings.sqlite_path)
        self._sqlite_store = sqlite_store
        event_store = EventStore(sqlite_store)
        event_writer = EventWriter(event_bus, event_store)
        event_writer.start()

        # Real-time alert push to the monitor (subscribe before rules publish).
        alert_broadcaster = None
        try:
            from alerts.alert_broadcaster import AlertBroadcaster
            alert_broadcaster = AlertBroadcaster(event_bus)
            alert_broadcaster.bind_loop(loop)
            alert_broadcaster.start()
        except Exception:
            logger.exception("AlertBroadcaster disabled")

        # ---- rules (posture-aware) ---------------------------------
        rule_engine = None
        event_enricher = None
        if posture_tracker is not None:
            try:
                from rules.rule_engine import RuleEngine
                from rules.rule_context import RuleContext
                from events.event_enricher import EventEnricher
                ctx = RuleContext(
                    frames=frames, detections=detection_buffer, tracks=track_buffer,
                    poses=pose_buffer, postures=posture_buffer,
                    posture_tracker=posture_tracker, identities=identity_buffer)
                event_enricher = EventEnricher()
                rule_engine = RuleEngine(ctx, event_bus, enricher=event_enricher)
                rule_engine.start()
            except Exception:
                logger.exception("RuleEngine disabled")

        # ---- cloud forward (recipient falls -> saveAlert/saveSnapshot) ----
        cloud_alert_publisher = None
        try:
            from alerts.cloud_alert_publisher import CloudAlertPublisher
            cloud_alert_publisher = CloudAlertPublisher(event_bus)
            cloud_alert_publisher.start()
        except Exception:
            logger.exception("CloudAlertPublisher disabled")

        # ---- expose to the API layer -------------------------------
        self._state = {
            "camera_manager": camera_manager,
            "detection_buffer": detection_buffer,
            "detection_runner": detection_runner,
            "track_buffer": track_buffer,
            "pose_buffer": pose_buffer,
            "posture_buffer": posture_buffer,
            "identity_buffer": identity_buffer,
            "gallery": gallery,
            "target_registry": target_registry,
            "enroll_worker": enroll_worker,
            "event_bus": event_bus,
            "event_store": event_store,
            "event_enricher": event_enricher,
            "alert_broadcaster": alert_broadcaster,
            "metrics_registry": metrics_registry,
            "system_monitor": system_monitor,
        }
        # stopped in this order on shutdown (reverse of dependency)
        self._shutdown = [
            cloud_alert_publisher, alert_broadcaster, rule_engine, event_writer,
            enroll_worker, reid_runner, pose_runner, tracking_runner, detection_runner,
        ]

        for url in lan_urls():
            logger.info("CERAVIS reachable at %s", url)

    def attach(self, state) -> None:
        for key, value in self._state.items():
            setattr(state, key, value)

    def stop(self) -> None:
        for runner in self._shutdown:
            if runner is None:
                continue
            try:
                runner.stop()
                runner.join(timeout=5)
            except Exception:
                logger.exception("clean shutdown failed")
        if self._system_monitor:
            self._system_monitor.stop()
        if self._camera_manager:
            self._camera_manager.stop_all()
        if self._sqlite_store:
            self._sqlite_store.close()
        logger.info("CERAVIS edge stopped")
