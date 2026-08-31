from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from bootstrap.pipeline import Pipeline
from maintenance import reboot

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
            from integration.edge_provision import apply_edge_id_verified
            eid = effective_edge_id()
            if eid:
                logger.info("boot: applying edge_id %s to frpc", eid)
                apply_edge_id_verified(eid)
        except Exception:
            logger.warning("edge_id boot-apply skipped", exc_info=True)

    import threading
    threading.Thread(target=_run, daemon=True, name="edge-id-boot").start()


def _ensure_camera_labels_on_boot() -> None:
    """Make sure every stored camera has its CAM_nn device label before anything
    reads cameras.json. Idempotent and normally a no-op — it exists so a device
    upgraded in the field labels its existing cameras once, instead of pushing an
    empty `device` to the cloud until each one is re-saved by hand. Best-effort:
    a storage problem here must never stop the API coming up."""
    try:
        from configuration.camera_config import CameraConfig
        changed = CameraConfig().ensure_device_labels()
        if changed:
            logger.info("boot: assigned device labels to %d camera(s)", changed)
    except Exception:
        logger.warning("camera device-label migration skipped", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_camera_labels_on_boot()   # cameras.json normalized before any read
    pipeline = Pipeline()
    pipeline.start()
    pipeline.attach(app.state)
    _apply_edge_id_on_boot()          # sync frpc to the account's edge_id
    # If a reboot brought us down, say so on the way back up — otherwise a
    # restart is indistinguishable from a crash in the logs and on the console.
    reboot.boot_report()
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
async def _no_cache_ui_assets(request, call_next):
    """Serve the UI code (HTML/CSS/JS) with `no-cache` so a browser ALWAYS
    revalidates and picks up a redeploy instead of running a stale cached bundle
    — the exact failure that made a freshly-pushed setup.html/ceravis.js appear
    broken (old JS, new HTML) on the device. Revalidation is a cheap 304 on the
    LAN; images keep normal caching. (Not encryption — just cache correctness.)"""
    resp = await call_next(request)
    path = request.url.path
    if path.endswith((".js", ".css", ".html")) or path.rstrip("/").endswith("/ui"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


class _FleetEdgePrefix:
    """Fleet per-edge path handling — a PURE-ASGI middleware on purpose.

    In the fleet every request arrives path-prefixed with this device's
    /<edge_id> (frps routes each home by that prefix — it routes ONLY by URL,
    never the request body). We strip our own prefix so the app's normal /api
    and /ui routes match, and we normalise a trailing-slash page url.

    Pure ASGI rather than @app.middleware("http") so the strip happens before
    routing for EVERY scope type, with none of Starlette's per-request
    request/response wrapping on a path that runs on every single call.

    LAN-direct requests carry no prefix -> no-op. effective_edge_id() is an
    in-memory resolve. The body `edge_id` stays as a second check (control_auth)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        # 308 `…/page.html/` -> `…/page.html` (http only) so a page's relative
        # assets (fleet-prefix.js, ceravis.js, css) resolve against the file, not
        # a phantom directory. Done BEFORE the strip, so the full /<edge_id> stays.
        if scope["type"] == "http" and path.endswith(".html/"):
            dest = path.rstrip("/")
            qs = scope.get("query_string", b"")
            if qs:
                dest += "?" + qs.decode("latin-1")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", dest.encode("latin-1")),
                                    (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        from configuration.account_config import effective_edge_id
        eid = effective_edge_id()
        if eid:
            prefix = "/" + eid
            if path == prefix or path.startswith(prefix + "/"):
                new = path[len(prefix):] or "/"
                scope = {**scope, "path": new, "raw_path": new.encode("utf-8")}
        await self.app(scope, receive, send)


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


# Added LAST so it wraps everything — the per-edge prefix must be stripped (for
# http AND websocket) before any routing or the other middleware run.
app.add_middleware(_FleetEdgePrefix)


for _router in (account_router, camera_router, zone_router, recipient_router,
                metrics_router, event_router, ai_router, recording_router,
                discovery_router, network_router, system_router):
    app.include_router(_router)

# Static UI (dashboard, cameras, zones) served same-origin.
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")
