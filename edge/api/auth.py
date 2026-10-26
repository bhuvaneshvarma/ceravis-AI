"""
Who may call this device's API — enforced on the SERVER, for every request.

Before this, the email + one-time-code sign-in existed only in the browser (a
note in localStorage), and the API answered anyone who could reach it: on the
home Wi-Fi, on the camera hotspot, or through the tunnel with the edge_id in
the URL. Now one gate in front of every /api route decides, closed by default:

  * a SIGNED-IN browser  — the session cookie this module issues the moment the
                           sign-in endpoint answers verified: true;
  * this device ITSELF   — loopback callers that did not come through the
                           tunnel (the fleet agent, tools.status, scripts);
  * the CLOUD routes     — the endpoints the app server and the carers' app call
                           (PTZ, camera control, recordings, talk-back), which
                           authenticate by the edge_id match in their own
                           handler (api/control_auth) exactly as before;
  * the PUBLIC routes    — the sign-in endpoints themselves, the health check,
                           the session check and the first-run setup status.

Everything else gets 401. A new route is protected without anyone remembering
to protect it.

The sign-in flow itself is untouched: the session is attached to the verify
endpoint's own answer, so how the edge_id is fetched, saved and applied does not
change at all.

Sessions: a random 256-bit token in an HttpOnly, SameSite=Strict cookie, stored
only as its SHA-256 (data/ui_sessions.json, 0600), surviving the service restart
a care-recipient switch performs. After UI_SESSION_IDLE_MINUTES without an
action on the page (a click, key, touch, scroll or mouse move — polling and a
playing video are not actions), the page asks "Are you still watching?" for
PROMPT_SECS; no answer signs the browser out (0 = never asks).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.control_auth import FLEET_EDGE_KEY
from common.atomic import write_json_atomic
from common.local_key import HEADER as LOCAL_KEY_HEADER, local_key
from config.settings import settings

logger = logging.getLogger("auth")

COOKIE = "ceravis_session"
_MAX_AGE_SECS = 30 * 86400          # hard ceiling on any session, idle limit or not
_SAVE_EVERY_SECS = 60.0             # activity bumps are written at most this often
# The idle limit as the page and this server share it: the page asks for
# PROMPT_SECS once the limit is reached, and reports actions at most every
# REPORT_SECS — so the server itself refuses a session only after the limit
# plus both, never underneath an open question.
PROMPT_SECS = 15
REPORT_SECS = 30


def _idle_secs() -> float:
    """No action for this long = the page asks; 0 = never."""
    return max(0.0, float(settings.ui_session_idle_minutes) * 60.0)

_PUBLIC = [
    ("GET", r"/api/v1/session"), ("POST", r"/api/v1/session/activity"),
    ("DELETE", r"/api/v1/session"),
    ("POST", r"/api/v1/account/(request-otp|verify)"),
    ("GET", r"/api/v1/account"),            # reduced answer unless signed in
    ("GET", r"/api/v1/setup/state"),        # first-run status; reduced once set up
]
# Authenticated by the edge_id match inside each handler (api/control_auth).
_CLOUD = [
    ("POST", r"/api/v1/cameras/(ptz|control)"),
    ("GET", r"/api/v1/recordings/(status|timeline)"),
    ("GET", r"/api/v1/recordings/[^/]+/(timeline|playback\.m3u8|snapshot|segment/[^/]+)"),
    ("GET", r"/api/v1/talkback"),
    ("*", r"/api/v1/talkback/(cameras|log|health|credential|check)"),
    ("*", r"/api/v1/talkback/[^/]+/(credential|test|stream)"),
]
# What a loopback caller WITHOUT the local key may still read: the health
# surface the fleet agent forwards (edge/fleet) — read-only, nothing else.
_LOCAL_READ = [("GET", r"/api/v1/system/status")]
_PUBLIC_RE = [(m, re.compile(p + r"/?")) for m, p in _PUBLIC]
_LOCAL_READ_RE = [(m, re.compile(p + r"/?")) for m, p in _LOCAL_READ]
_CLOUD_RE = [(m, re.compile(p + r"/?")) for m, p in _CLOUD]
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _matches(rules, method: str, path: str) -> bool:
    return any((m == "*" or m == method) and rx.fullmatch(path) for m, rx in rules)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """Signed-in browsers, by token digest. Thread-safe; persisted so a service
    restart does not sign everyone out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._loaded = False
        self._dirty_since = 0.0

    @property
    def _file(self):
        return settings.data_path / "ui_sessions.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._file.exists():
                self._sessions = dict(json.loads(self._file.read_text()))
        except Exception:
            logger.warning("unreadable %s — every browser signs in again", self._file,
                           exc_info=True)
            self._sessions = {}

    def _save(self) -> None:
        try:
            write_json_atomic(self._file, self._sessions, mode=0o600, indent=None)
            self._dirty_since = 0.0
        except Exception:
            logger.warning("could not save sessions", exc_info=True)

    def _expired(self, s: dict, now: float) -> bool:
        idle = _idle_secs()
        return (now - s["created"] > _MAX_AGE_SECS
                or (idle > 0 and now - s["active"] > idle + PROMPT_SECS + REPORT_SECS))

    def issue(self, email: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._load()
            self._sessions = {k: v for k, v in self._sessions.items()
                              if not self._expired(v, now)}
            self._sessions[_digest(token)] = {"created": now, "active": now,
                                              "email": email}
            self._save()
        return token

    def check(self, token: str | None) -> dict | None:
        """The live session for this token, or None. Reading never extends it."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._load()
            s = self._sessions.get(_digest(token))
            if s is None:
                return None
            if self._expired(s, now):
                self._sessions.pop(_digest(token), None)
                self._save()
                return None
            return dict(s)

    def touch(self, token: str | None) -> dict | None:
        """A user action (click, key, page open) — restart the idle clock."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._load()
            s = self._sessions.get(_digest(token))
            if s is None or self._expired(s, now):
                return None
            s["active"] = now
            self._dirty_since = self._dirty_since or now
            if now - self._dirty_since >= _SAVE_EVERY_SECS:
                self._save()
            return dict(s)

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._load()
            if self._sessions.pop(_digest(token), None) is not None:
                self._save()


sessions = SessionStore()


def remaining_secs(s: dict) -> int | None:
    """Seconds until the page asks "still watching?" (None = no limit)."""
    idle = _idle_secs()
    if idle <= 0:
        return None
    return max(0, int(s["active"] + idle - time.time()))


# ---- request helpers --------------------------------------------------
def _cookie(scope: dict) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and v:
                    return v
    return None


def _header(scope: dict, key: bytes) -> bytes | None:
    for name, value in scope.get("headers") or []:
        if name == key:
            return value
    return None


def _loopback(scope: dict) -> bool:
    """A request from this device itself — never one that came in through the
    tunnel (frpc also connects from loopback, but tunnel requests carry the
    /<edge_id> prefix and the cloud proxy's X-Forwarded-For)."""
    client = (scope.get("client") or ("", 0))[0]
    return (client in _LOOPBACK and FLEET_EDGE_KEY not in scope
            and _header(scope, b"x-forwarded-for") is None)


def is_local(scope: dict) -> bool:
    """This device calling itself WITH the device's local key (common/
    local_key) — our own tools and scripts. Reaching 127.0.0.1 alone proves
    nothing (any process on the box can, and so can a browser opened on the
    device), so without the key a loopback caller signs in like anyone else;
    only the read-only health surface (_LOCAL_READ) is open to it."""
    return _loopback(scope) and local_key.matches(
        _header(scope, LOCAL_KEY_HEADER.lower().encode("latin-1")))


def session_of(request: Request) -> dict | None:
    return sessions.check(_cookie(request.scope))


def signed_in(request: Request) -> bool:
    return is_local(request.scope) or session_of(request) is not None


def _cookie_header(scope: dict, token: str, max_age: int) -> bytes:
    """Path-scoped: through the tunnel every home shares one domain, so the
    cookie is bound to THIS home's /<edge_id>/ path (the edge_id as it stands
    AFTER sign-in, so a care-recipient switch keeps the browser signed in)."""
    fleet = FLEET_EDGE_KEY in scope
    path = "/"
    if fleet:
        from config.account_config import effective_edge_id
        path = f"/{effective_edge_id() or scope[FLEET_EDGE_KEY]}/"
    # Secure wherever the page itself is: the tunnel (the fleet domain's HTTPS)
    # and the LAN listener's HTTPS (serve.py) — never sent over plain HTTP then.
    secure = fleet or scope.get("scheme") in ("https", "wss")
    parts = [f"{COOKIE}={token}", f"Path={path}", f"Max-Age={max_age}",
             "HttpOnly", "SameSite=Strict"] + (["Secure"] if secure else [])
    return "; ".join(parts).encode("latin-1")


# ---- the gate (pure ASGI, inside the fleet-prefix strip) ---------------
class SessionGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET") if scope["type"] == "http" else "GET"
        allowed = (method == "OPTIONS"
                   or is_local(scope)
                   or (_loopback(scope) and _matches(_LOCAL_READ_RE, method, path))
                   or _matches(_PUBLIC_RE, method, path)
                   or _matches(_CLOUD_RE, method, path)
                   or sessions.check(_cookie(scope)) is not None)
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            body = b'{"detail":"Sign in required","signin":true}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if method == "POST" and path.rstrip("/") == "/api/v1/account/verify":
            return await self._verify(scope, receive, send)
        return await self.app(scope, receive, send)

    async def _verify(self, scope, receive, send):
        """Pass the sign-in through unchanged; if it answers verified: true,
        attach a fresh session cookie to that same answer."""
        start: dict | None = None
        chunks: list[bytes] = []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                headers = list(start.get("headers") or [])
                if start["status"] == 200:
                    try:
                        answer = json.loads(body)
                    except ValueError:
                        answer = {}
                    if isinstance(answer, dict) and answer.get("verified") is True:
                        user = answer.get("user") or {}
                        email = user.get("email", "") if isinstance(user, dict) else ""
                        token = sessions.issue(email)
                        headers.append((b"set-cookie",
                                        _cookie_header(scope, token, _MAX_AGE_SECS)))
                        logger.info("signed in: %s", email or "(no email)")
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, capture)


# ---- endpoints ----------------------------------------------------------
router = APIRouter(prefix="/api/v1/session", tags=["session"])


def _state(s: dict | None, local: bool) -> dict:
    return {"signedIn": bool(s) or local,
            "idleMinutes": settings.ui_session_idle_minutes,
            "promptSecs": PROMPT_SECS, "reportSecs": REPORT_SECS,
            "remainingSecs": remaining_secs(s) if s else None}


@router.get("")
def session_state(request: Request) -> dict:
    return _state(session_of(request), is_local(request.scope))


@router.post("/activity")
def session_activity(request: Request) -> dict:
    """The page saw an action, or "still watching?" was answered — the idle
    clock starts over."""
    return _state(sessions.touch(_cookie(request.scope)), is_local(request.scope))


@router.delete("")
def sign_out(request: Request) -> JSONResponse:
    sessions.revoke(_cookie(request.scope))
    resp = JSONResponse({"signedIn": False})
    resp.headers.append("set-cookie",
                        _cookie_header(request.scope, "", 0).decode("latin-1"))
    return resp
