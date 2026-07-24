from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from bootstrap.pipeline import Pipeline

from api.account_routes import router as account_router
from api.camera_routes import router as camera_router
from api.zone_routes import router as zone_router
from api.recipient_routes import router as recipient_router
from api.metrics_routes import router as metrics_router
from api.event_routes import router as event_router
from api.ai_routes import router as ai_router
from api.discovery_routes import router as discovery_router
from api.network_routes import router as network_router
from api.recording_routes import router as recording_router
from api.system_routes import router as system_router


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("ceravis")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pipeline = Pipeline()
    pipeline.start()
    pipeline.attach(app.state)
    logger.info("CERAVIS edge ready")
    yield
    pipeline.stop()


app = FastAPI(title="CERAVIS Edge API", version=settings.app_version, lifespan=lifespan)

# Allow external frontends (a separate web/mobile app on another origin) to call
# this edge API over the LAN. The device serves only on the private network;
# allow-all keeps cross-origin clients (and the built-in UI) working config-free.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)


# Paths that would flood or loop the monitor console — skipped from the log.
_LOG_SKIP = {"/api/v1/cloud/activity",        # the console reading itself
             "/api/v1/cameras/status",        # camera-dot poll (~20 s)
             "/api/v1/system/status",         # backbone poll
             "/api/v1/recordings/state"}       # recording-switch poll


@app.middleware("http")
async def _log_inbound_calls(request, call_next):
    """Mirror every inbound API hit into the monitor's Cloud Sync Console, so it
    shows BOTH directions: the edge's calls OUT to ceravishealth.in
    (userDetails / saveCamera / saveAlert / saveSnapshot, logged by the client)
    AND anyone hitting THIS device on the edgeai domain (PTZ, playback, camera
    CRUD, account, …). High-frequency polls, HLS segment fetches and PTZ (logged
    with richer detail) are skipped so the console stays readable."""
    t0 = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if (request.method != "OPTIONS" and path.startswith("/api/v1/")
            and path not in _LOG_SKIP and "/segment/" not in path
            and not path.endswith("/ptz")):
        try:
            from integration import call_log
            call_log.record("api", 200 <= response.status_code < 400,
                            status=response.status_code,
                            latency_ms=(time.perf_counter() - t0) * 1000,
                            label=f"{request.method} {path}")
        except Exception:
            pass
    return response


for _router in (account_router, camera_router, zone_router, recipient_router,
                metrics_router, event_router, ai_router, recording_router,
                discovery_router, network_router, system_router):
    app.include_router(_router)

# Static UI (dashboard, cameras, zones) served same-origin.
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")
