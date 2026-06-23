from __future__ import annotations

import logging
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config.settings import settings


def _lan_urls(port: int = 8000) -> list[str]:
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
        host = socket.gethostname()
        urls.append(f"http://{host}.local:{port}")
    except Exception:
        pass
    return urls

from api.camera_routes import router as camera_router
from api.recipient_routes import router as recipient_router
from api.zone_routes import router as zone_router
from api.metrics_routes import router as metrics_router
from api.event_routes import router as event_router
from api.ai_routes import router as ai_router
from api.account_routes import router as account_router

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
from streaming.websocket_stream import mjpeg_camera, stream_camera


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
    from tracking.track_feature_buffer import TrackFeatureBuffer
    feature_buffer = TrackFeatureBuffer()      # per-track OSNet features (tracker -> reid)
    pose_buffer = PoseBuffer()
    posture_buffer = PostureBuffer()
    identity_buffer = IdentityBuffer()

    # ReID-driven "who is the target" lock, shared by pose + reid runners.
    from reid.target_registry import TargetRegistry
    target_registry = TargetRegistry()

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

    # ---- tracking (clean-room BoT-SORT + OSNet appearance) -----
    tracking_runner = None
    try:
        from tracking.tracking_runner import TrackingRunner
        tracking_runner = TrackingRunner(
            detection_buffer, track_buffer,
            frame_buffer=camera_manager.frame_buffer,
            feature_buffer=feature_buffer,
            metrics_registry=metrics_registry,
        )
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
            target_registry=target_registry,
        )
        pose_runner.start()
        posture_tracker = pose_runner.posture_tracker
    except Exception:
        logger.exception("PoseRunner disabled")

    # ---- reid gallery (shared by ReID runner + enrollment worker) ----
    gallery: FaissGallery | None = None
    try:
        gallery = FaissGallery()
    except Exception:
        logger.exception("FAISS gallery unavailable (ReID + enrollment off)")

    # ---- reid (FastReid) --------------------------------------
    reid_runner = None
    if gallery is not None:
        try:
            from reid.reid_runner import ReIDRunner
            reid_runner = ReIDRunner(
                track_buffer=track_buffer,
                feature_buffer=feature_buffer,
                identity_buffer=identity_buffer,
                gallery=gallery,
                target_registry=target_registry,
                posture_buffer=posture_buffer,
            )
            reid_runner.start()
        except Exception:
            logger.exception("ReIDRunner disabled")

    # ---- enrollment worker (stores media -> embeddings -> FAISS) ----
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
    event_store = EventStore(sqlite_store)
    event_writer = EventWriter(event_bus, event_store)
    event_writer.start()

    # Real-time alert push to the monitor (subscribe before the rules start
    # publishing). Bridges the threaded bus to async WebSocket clients.
    alert_broadcaster = None
    try:
        import asyncio
        from alerts.alert_broadcaster import AlertBroadcaster
        alert_broadcaster = AlertBroadcaster(event_bus)
        alert_broadcaster.bind_loop(asyncio.get_running_loop())
        alert_broadcaster.start()
    except Exception:
        logger.exception("AlertBroadcaster disabled")

    # ---- rules (posture-aware) --------------------------------
    rule_engine = None
    event_enricher = None
    if posture_tracker is not None:
        try:
            from rules.rule_engine import RuleEngine
            from rules.rule_context import RuleContext
            from events.event_enricher import EventEnricher
            ctx = RuleContext(
                frames=camera_manager.frame_buffer,
                detections=detection_buffer,
                tracks=track_buffer,
                poses=pose_buffer,
                postures=posture_buffer,
                posture_tracker=posture_tracker,
                identities=identity_buffer,
            )
            # Enriches each event with room/area + an annotated snapshot +
            # severity (zone-aware fall: lying on a bed/couch is not an alarm).
            event_enricher = EventEnricher()
            rule_engine = RuleEngine(ctx, event_bus, enricher=event_enricher)
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

    # Forward critical/warning alerts to the CERAVIS app server (saveAlert).
    cloud_alert_publisher = None
    try:
        from alerts.cloud_alert_publisher import CloudAlertPublisher
        cloud_alert_publisher = CloudAlertPublisher(event_bus)
        cloud_alert_publisher.start()
    except Exception:
        logger.exception("CloudAlertPublisher disabled")

    # ---- expose ------------------------------------------------
    app.state.camera_manager = camera_manager
    app.state.detection_buffer = detection_buffer
    app.state.detection_runner = detection_runner
    app.state.track_buffer = track_buffer
    app.state.pose_buffer = pose_buffer
    app.state.posture_buffer = posture_buffer
    app.state.identity_buffer = identity_buffer
    app.state.gallery = gallery
    app.state.target_registry = target_registry
    app.state.enroll_worker = enroll_worker
    app.state.event_bus = event_bus
    app.state.event_store = event_store
    app.state.event_enricher = event_enricher
    app.state.alert_broadcaster = alert_broadcaster
    app.state.metrics_registry = metrics_registry
    app.state.system_monitor = system_monitor

    for url in _lan_urls():
        logger.info("CERAVIS reachable at %s", url)
    logger.info("CERAVIS edge ready")
    yield

    # ---- shutdown ----------------------------------------------
    for runner in (
        mqtt_publisher, cloud_alert_publisher, alert_broadcaster, rule_engine,
        event_writer, enroll_worker,
        reid_runner, pose_runner, tracking_runner, detection_runner,
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

# Allow external frontends (a separate web/mobile app on another origin) to
# call this edge API over the LAN. The device serves only on the private
# network; allow-all keeps cross-origin clients (and the built-in UI from any
# host) working without per-origin config.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(account_router)
app.include_router(camera_router)
app.include_router(zone_router)
app.include_router(recipient_router)
app.include_router(metrics_router)
app.include_router(event_router)
app.include_router(ai_router)

# Static UI (dashboard, cameras, zones) served same-origin
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")


@app.get("/")
def root():
    return RedirectResponse("/ui/live.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "device_id": settings.device_id,
    }


@app.websocket("/stream/{camera_id}")
async def websocket_stream(websocket: WebSocket, camera_id: str):
    await stream_camera(
        websocket,
        websocket.app.state.camera_manager.frame_buffer,
        camera_id,
    )


def _slugify(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _resolve_camera(label: str) -> str:
    """A stream label may be the camera id, its display name, or its room
    (slugified) — resolve any of them to the actual camera id."""
    try:
        from configuration.camera_config import CameraConfig
        cams = CameraConfig().get_all()
    except Exception:
        return label
    for c in cams:                                   # exact id wins
        if c.camera_id == label:
            return c.camera_id
    sl = _slugify(label)
    for c in cams:
        if sl and sl in (_slugify(c.camera_name), _slugify(c.room_name),
                         _slugify(c.camera_id)):
            return c.camera_id
    return label


@app.get("/stream.mjpeg/{label}")
async def mjpeg_stream(label: str, request: Request):
    """HTTP(S) MJPEG live stream. `label` may be the camera id, its display name,
    or its room (slugified) — all resolve to the same camera. Embeddable in an
    <img>/browser and the link handed to the cloud monitor."""
    frame_buffer = request.app.state.camera_manager.frame_buffer
    return StreamingResponse(
        mjpeg_camera(frame_buffer, _resolve_camera(label)),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.websocket("/alerts/stream")
async def websocket_alerts(websocket: WebSocket):
    """Real-time push of enriched events/alerts to the monitor console."""
    await websocket.accept()
    bc = getattr(websocket.app.state, "alert_broadcaster", None)
    if bc is None:
        await websocket.close()
        return
    q = await bc.register()
    try:
        while True:
            payload = await q.get()
            await websocket.send_json(payload)
    except Exception:
        pass
    finally:
        await bc.unregister(q)
