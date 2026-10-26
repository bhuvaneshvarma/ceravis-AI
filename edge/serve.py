"""
The CERAVIS web server — ONE process, ONE app, two listeners:

  http://0.0.0.0:8000    this device itself and the remote tunnel (frpc reaches
                         the app over loopback; the fleet domain's HTTPS ends at
                         the cloud proxy). A browser on the LAN asking here is
                         sent to the HTTPS listener (308), so nothing a carer
                         types on the home network travels unencrypted.
  https://0.0.0.0:8443   the LAN, encrypted with this device's own certificate
                         (data/certs — made per device at first boot, the same
                         one the media server uses). A secure page is also what
                         lets talk-back use the microphone on the LAN.

Without a certificate — or while the HTTPS listener is not up — the first
listener serves the LAN itself (never a redirect to nowhere). The app starts once (the HTTPS listener runs
with lifespan off and opens only after the app is up); the first listener owns
the signals, so a stop shuts both down in the app's own order.

Run (ceravis.service does):  python -m serve
"""
from __future__ import annotations

from common.process_env import cap_math_threads

cap_math_threads()      # before anything imports numpy (common/process_env)

import asyncio          # noqa: E402
import contextlib
import inspect
import logging
import os
from pathlib import Path

import uvicorn

from config.settings import settings

logger = logging.getLogger("serve")

HTTP_PORT = 8000
HTTPS_PORT = 8443
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class LanToHttps:
    """ASGI wrapper for the plain-HTTP listener: a LAN browser is redirected to
    the same address on HTTPS. Loopback callers (this device, the tunnel) and a
    request a local proxy forwarded (X-Forwarded-For) are served as they are."""

    def __init__(self, app, https_port: int, ready=lambda: True) -> None:
        self.app, self.https_port, self.ready = app, https_port, ready

    async def __call__(self, scope, receive, send):
        if (scope["type"] not in ("http", "websocket") or not self._lan(scope)
                or not self.ready()):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        host = (_header(scope, b"host") or "").rsplit(":", 1)[0].strip("[]") \
            or (scope.get("server") or ("localhost",))[0]
        host = f"[{host}]" if ":" in host else host
        query = scope.get("query_string", b"").decode("latin-1")
        target = f"https://{host}:{self.https_port}{scope.get('path', '/')}" \
                 + (f"?{query}" if query else "")
        await send({"type": "http.response.start", "status": 308,
                    "headers": [(b"location", target.encode("latin-1")),
                                (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    def _lan(scope) -> bool:
        client = (scope.get("client") or ("", 0))[0]
        return client not in _LOOPBACK and _header(scope, b"x-forwarded-for") is None


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return None


class _Second(uvicorn.Server):
    """The HTTPS listener: the first server owns the signals and the app's
    startup and shutdown."""

    @contextlib.contextmanager
    def capture_signals(self):            # uvicorn >= 0.29
        yield

    def install_signal_handlers(self) -> None:   # older uvicorn
        pass


def _config(app, **kw) -> uvicorn.Config:
    # Same as the uvicorn command line had: proxy headers from loopback only,
    # and a bounded wait on open connections so the ordered shutdown runs (the
    # unit sets it: UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN).
    if "timeout_graceful_shutdown" in inspect.signature(uvicorn.Config).parameters:
        kw.setdefault("timeout_graceful_shutdown",
                      int(os.environ.get("UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN") or 10))
    return uvicorn.Config(app, proxy_headers=True, forwarded_allow_ips="127.0.0.1", **kw)


def certificate() -> tuple[Path, Path] | None:
    certs = Path(settings.mediamtx_cert_dir)
    crt, key = certs / "server.crt", certs / "server.key"
    return (crt, key) if crt.is_file() and key.is_file() else None


async def _listen(server: uvicorn.Server) -> None:
    """The HTTPS listener, never fatal: a port in use or an unreadable
    certificate stops only it (uvicorn would exit the process) — the LAN is
    then served on plain HTTP again."""
    try:
        await server.serve()
    except (SystemExit, Exception):
        logger.exception("HTTPS listener stopped — the LAN is served on plain HTTP")


class WebServer:
    """The app's two listeners (module docstring)."""

    def __init__(self, app, http_port: int = HTTP_PORT, https_port: int = HTTPS_PORT,
                 cert: tuple[Path, Path] | None = None) -> None:
        self.app, self.cert = app, cert or certificate()
        self.https_port = https_port
        self.http = uvicorn.Server(_config(LanToHttps(app, https_port, self.https_up),
                                           host="0.0.0.0", port=http_port))
        self.https: uvicorn.Server | None = None
        self._https_task: asyncio.Task | None = None

    def https_up(self) -> bool:
        return (self.https is not None and self.https.started
                and self._https_task is not None and not self._https_task.done())

    async def run(self) -> None:
        main = asyncio.create_task(self.http.serve())
        if self.cert is None:
            logger.warning("no certificate in %s — the LAN is served on plain HTTP only",
                           settings.mediamtx_cert_dir)
            await main
            return
        while not self.http.started and not main.done():     # the app is up first
            await asyncio.sleep(0.1)
        if not main.done():
            crt, key = self.cert
            self.https = _Second(_config(self.app, host="0.0.0.0", port=self.https_port,
                                         lifespan="off", ssl_certfile=str(crt),
                                         ssl_keyfile=str(key)))
            self._https_task = asyncio.create_task(_listen(self.https))
            logger.info("LAN: https://<this device>:%d (plain HTTP from the LAN "
                        "redirects there)", self.https_port)
        await main
        if self.https is not None:
            self.https.should_exit = True
            await self._https_task


def _loop_runner():
    """uvloop when installed (uvicorn[standard] brings it), as the uvicorn
    command line used."""
    try:
        import uvloop
        return getattr(uvloop, "run", None) or asyncio.run
    except ImportError:
        return asyncio.run


if __name__ == "__main__":
    from main import app
    _loop_runner()(WebServer(app).run())
