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


def _apply_edge_id_on_boot() -> None:
    """(Re)apply this device's edge_id to the frp tunnel on startup, so a
    `systemctl restart ceravis` reliably points frpc at the LATEST verified
    device token (from account.json / jetson.env). Runs in a daemon thread — the
    privileged helper restarts frpc, which must not block the API coming up.
    Best-effort: no account or no installed helper just logs (see
    integration.edge_provision — install it with `bash cloud/install_frpc.sh`)."""
    def _run():
        try:
            from configuration.account_config import effective_edge_id
            from integration.edge_provision import apply_edge_id
            eid = effective_edge_id()
            if eid:
                logger.info("boot: applying edge_id %s to frpc", eid)
                apply_edge_id(eid)
        except Exception:
            logger.warning("edge_id boot-apply skipped", exc_info=True)

    import threading
    threading.Thread(target=_run, daemon=True, name="edge-id-boot").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pipeline = Pipeline()
    pipeline.start()
    pipeline.attach(app.state)
    _apply_edge_id_on_boot()          # sync frpc to the account's edge_id
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
    # Custom response headers a browser/webview caller must be able to READ
    # cross-origin — the snapshot's real instant + source (native apps ignore
    # CORS, but a JS/webview client can't see these without this).
    expose_headers=["X-Snapshot-Time", "X-Snapshot-Source",
                    "X-Requested-Time", "X-Snapshot-Delta-Ms"],
)


@app.middleware("http")
async def _strip_fleet_edge_prefix(request, call_next):
    """Make the API multi-device safe. In the fleet, frps routes ONLY by URL, so
    every home's control call must carry a per-device key in the PATH — the same
    /<edge_id> prefix the live links use (…/<edge_id>/api/v1/…). frps hands each
    home's calls to the right edge on that unique prefix; here we strip this
    device's own prefix so the app's routes (/api, /ui, /stream) still match.

    LAN-direct calls carry NO prefix and are untouched, so both the local UI and
    the fleet reach the same handlers. The body `edge_id` stays as a second check
    (control_auth) — routing puts the call on the right edge, the check confirms
    it. Cheap: effective_edge_id() is an in-memory resolve."""
    from configuration.account_config import effective_edge_id
    eid = effective_edge_id()
    if eid:
        path = request.scope.get("path", "")
        prefix = "/" + eid
        if path == prefix or path.startswith(prefix + "/"):
            stripped = path[len(prefix):] or "/"
            request.scope["path"] = stripped
            request.scope["raw_path"] = stripped.encode("utf-8")
    return await call_next(request)


def _inbound_endpoint(path: str) -> str | None:
    """Classify an inbound request into the ONLY kinds the Cloud Sync Console
    shows — the endpoints the app.ceravishealth.in backend calls on THIS device.
    Everything else (local UI CRUD, status polls, HLS segment fetches) returns
    None and is never logged, so the console stays a clean cloud-only view. PTZ
    is handled here too even though camera_routes logs it with richer detail —
    the label check keeps it a single entry."""
    if path.endswith("/timeline"):
        return "timeline"
    if "/playback" in path:
        return "playback"
    # Only the recordings still-frame (mobile/cloud live view), never the local
    # zone-labeler's /cameras/{id}/snapshot.
    if "/recordings/" in path and path.endswith("/snapshot"):
        return "snapshot"
    return None


@app.middleware("http")
async def _log_inbound_calls(request, call_next):
    """Mirror ONLY the backend-facing inbound calls into the monitor's Cloud Sync
    Console: the recording endpoints app.ceravishealth.in hits (playback,
    timeline). PTZ is logged with richer detail by camera_routes; the edge's
    OUTBOUND calls to ceravishealth.in (userDetails / saveCamera / saveAlert /
    saveSnapshot / getPatientPostures / uploadEmbeddingFile) are logged by the
    API client. All other inbound traffic is intentionally NOT logged."""
    t0 = time.perf_counter()
    response = await call_next(request)
    if request.method == "OPTIONS":
        return response
    endpoint = _inbound_endpoint(request.url.path)
    if endpoint:
        try:
            from integration import call_log
            # Request RECEIVED: method + path + query (these endpoints are GETs, no
            # body). Shorten edge_id so the console line stays readable.
            q = dict(request.query_params)
            if q.get("edge_id"):
                q["edge_id"] = "…" + q["edge_id"][-6:]
            req = {"method": request.method, "path": request.url.path, "query": q}
            # Response SENT: the outcome + shape, never the (huge/binary) body.
            h = response.headers
            resp = {"status": response.status_code,
                    "type": h.get("content-type"), "bytes": h.get("content-length")}
            call_log.record(endpoint, 200 <= response.status_code < 400,
                            direction="in", status=response.status_code,
                            latency_ms=(time.perf_counter() - t0) * 1000,
                            label=f"{request.method} {request.url.path}",
                            request=req, response=resp)
        except Exception:
            pass
    return response


for _router in (account_router, camera_router, zone_router, recipient_router,
                metrics_router, event_router, ai_router, recording_router,
                discovery_router, network_router, system_router):
    app.include_router(_router)

# Static UI (dashboard, cameras, zones) served same-origin.
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")
