from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from config.settings import settings

from api.camera_routes import router as camera_router
from api.recipient_routes import router as recipient_router
from api.zone_routes import router as zone_router
from api.metrics_routes import router as metrics_router

from ingestion.camera_manager import CameraManager
from detection.detection_buffer import DetectionBuffer
from detection.detection_runner import DetectionRunner
from tracking.track_buffer import TrackBuffer
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer
from reid.identity_buffer import IdentityBuffer
from reid.faiss_index import FaissGallery
from events.event_bus import EventBus
from storage.sqlite_store import SqliteStore
from storage.event_store import EventStore
from events.event_writer import EventWriter
from monitoring.pipeline_metrics import MetricsRegistry
from monitoring.system_monitor import SystemMonitor
from streaming.websocket_stream import stream_camera


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("ceravis")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- ingestion ---------------------------------------------
    camera_manager = CameraManager()
    camera_manager.start_all()

    # ---- buffers (always on) -----------------------------------
    detection_buffer = DetectionBuffer()
    track_buffer = TrackBuffer()
    pose_buffer = PoseBuffer()
    posture_buffer = PostureBuffer()
    identity_buffer = IdentityBuffer()

    # ---- monitoring --------------------------------------------
    metrics_registry = MetricsRegistry()
    system_monitor = SystemMonitor()
    system_monitor.start()

    # ---- detection (YOLO26m) -----------------------------------
    detection_runner = DetectionRunner(
        frame_buffer=camera_manager.frame_buffer,
        detection_buffer=detection_buffer,
        metrics_registry=metrics_registry,
    )
    try:
        detection_runner.start()
    except Exception:
        logger.exception("DetectionRunner failed to start")

    # ---- tracking (ByteTrack) ----------------------------------
    tracking_runner = None
    try:
        from tracking.tracking_runner import TrackingRunner
        tracking_runner = TrackingRunner(detection_buffer, track_buffer)
        tracking_runner.start()
    except Exception:
        logger.exception("TrackingRunner disabled")

    # ---- pose + posture (YOLO26m-Pose) -------------------------
    pose_runner = None
    posture_tracker = None
    try:
        from pose.pose_runner import PoseRunner
        pose_runner = PoseRunner(
            frame_buffer=camera_manager.frame_buffer,
            pose_buffer=pose_buffer,
            track_buffer=track_buffer,
            posture_buffer=posture_buffer,
            metrics_registry=metrics_registry,
        )
        pose_runner.start()
        posture_tracker = pose_runner.posture_tracker
    except Exception:
        logger.exception("PoseRunner disabled")

    # ---- reid (FastReid) --------------------------------------
    reid_runner = None
    gallery: FaissGallery | None = None
    try:
        gallery = FaissGallery()
        from reid.reid_runner import ReIDRunner
        reid_runner = ReIDRunner(
            frame_buffer=camera_manager.frame_buffer,
            track_buffer=track_buffer,
            identity_buffer=identity_buffer,
            gallery=gallery,
        )
        reid_runner.start()
    except Exception:
        logger.exception("ReIDRunner disabled")

    # ---- events + storage --------------------------------------
    event_bus = EventBus()
    sqlite_store = SqliteStore(settings.sqlite_path)
    event_store = EventStore(sqlite_store)
    event_writer = EventWriter(event_bus, event_store)
    event_writer.start()

    # ---- rules (posture-aware) --------------------------------
    rule_engine = None
    if posture_tracker is not None:
        try:
            from rules.rule_engine import RuleEngine
            from rules.rule_context import RuleContext
            ctx = RuleContext(
                frames=camera_manager.frame_buffer,
                detections=detection_buffer,
                tracks=track_buffer,
                poses=pose_buffer,
                postures=posture_buffer,
                posture_tracker=posture_tracker,
                identities=identity_buffer,
            )
            rule_engine = RuleEngine(ctx, event_bus)
            rule_engine.start()
        except Exception:
            logger.exception("RuleEngine disabled")

    # ---- alerts ------------------------------------------------
    mqtt_publisher = None
    try:
        from alerts.mqtt_publisher import MqttPublisher
        mqtt_publisher = MqttPublisher(event_bus)
        mqtt_publisher.start()
    except Exception:
        logger.exception("MqttPublisher disabled")

    # ---- expose ------------------------------------------------
    app.state.camera_manager = camera_manager
    app.state.detection_buffer = detection_buffer
    app.state.detection_runner = detection_runner
    app.state.track_buffer = track_buffer
    app.state.pose_buffer = pose_buffer
    app.state.posture_buffer = posture_buffer
    app.state.identity_buffer = identity_buffer
    app.state.gallery = gallery
    app.state.event_bus = event_bus
    app.state.event_store = event_store
    app.state.metrics_registry = metrics_registry
    app.state.system_monitor = system_monitor

    logger.info("CERAVIS edge ready")
    yield

    # ---- shutdown ----------------------------------------------
    for runner in (
        mqtt_publisher, rule_engine, event_writer, reid_runner,
        pose_runner, tracking_runner, detection_runner,
    ):
        if runner is None:
            continue
        try:
            runner.stop()
            runner.join(timeout=5)
        except Exception:
            logger.exception("clean shutdown failed")

    system_monitor.stop()
    camera_manager.stop_all()
    sqlite_store.close()
    logger.info("CERAVIS edge stopped")


app = FastAPI(
    title="CERAVIS Edge API",
    version=settings.app_version,
    lifespan=lifespan,
)

app.include_router(camera_router)
app.include_router(zone_router)
app.include_router(recipient_router)
app.include_router(metrics_router)

# Static UI (dashboard, cameras, zones) served same-origin
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")


@app.get("/")
def root():
    return RedirectResponse("/ui/dashboard.html")


@app.get("/health")
async def health():
    return {"status": "ok", "version": settings.app_version}


@app.websocket("/stream/{camera_id}")
async def websocket_stream(websocket: WebSocket, camera_id: str):
    await stream_camera(
        websocket,
        websocket.app.state.camera_manager.frame_buffer,
        camera_id,
    )
