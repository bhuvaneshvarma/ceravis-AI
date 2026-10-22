"""
The API sign-in gate (edge/api/auth.py) — every rule it enforces, driven with
raw ASGI requests (no web server, no browser):

  * a protected route answers 401 without a session, a public route does not
  * this device calling itself passes; the same call through the tunnel does not
  * the cloud routes (edge_id-checked in their own handlers) are not blocked
  * the sign-in answer {verified: true} gets a session cookie, {verified: false}
    does not — and the cookie then opens protected routes
  * through the tunnel the cookie is Secure and bound to /<edge_id>/
  * the idle limit signs a browser out; polling does not keep it alive, a user
    action does
  * sign-out revokes; a websocket without a session is closed with 4401
  * the session file holds only token digests, never a token

Run:  PYTHONPATH=edge python tests/test_api_auth.py
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

from config.settings import settings                      # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ceravis-auth-")
settings.data_dir = _TMP

from api import auth                                       # noqa: E402
from api.control_auth import FLEET_EDGE_KEY                # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


async def _app(scope, receive, send):
    """Stand-in for the real app: answers 200, and for the verify route the
    body the test asked for."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    body = scope.get("_answer", b'{"ok": true}')
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


gate = auth.SessionGate(_app)


def call(path, method="GET", client="192.168.0.20", cookie=None, headers=(),
         fleet=None, answer=None, kind="http"):
    scope = {"type": kind, "path": path, "method": method,
             "client": (client, 50000), "headers": list(headers)}
    if cookie:
        scope["headers"].append((b"cookie", f"{auth.COOKIE}={cookie}".encode()))
    if fleet:
        scope[FLEET_EDGE_KEY] = fleet
    if answer is not None:
        scope["_answer"] = json.dumps(answer).encode()
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(gate(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    status = start["status"] if start else None
    hdrs = dict((k.decode(), v.decode()) for k, v in (start or {}).get("headers", []))
    closed = next((m["code"] for m in sent if m["type"] == "websocket.close"), None)
    return status, hdrs, closed


def token_of(set_cookie: str) -> str:
    return set_cookie.split(";")[0].split("=", 1)[1]


print("\n1. closed by default")
check("a protected route without a session -> 401",
      call("/api/v1/cameras")[0] == 401)
check("the UI pages themselves are not API — served", call("/ui/live.html")[0] == 200)
for p, m in (("/api/v1/account/request-otp", "POST"), ("/api/v1/account/verify", "POST"),
             ("/api/v1/session", "GET"), ("/api/v1/setup/state", "GET"),
             ("/api/v1/account", "GET")):
    check(f"public: {m} {p}", call(p, m, answer={"verified": False})[0] == 200)
check("a CORS pre-flight is never blocked", call("/api/v1/cameras", "OPTIONS")[0] == 200)

print("\n2. this device calling itself")
check("loopback, no tunnel -> allowed", call("/api/v1/system/status", client="127.0.0.1")[0] == 200)
check("loopback THROUGH the tunnel (/<edge_id> prefix) -> 401",
      call("/api/v1/system/status", client="127.0.0.1", fleet="E1")[0] == 401)
check("loopback carrying X-Forwarded-For (the cloud proxy) -> 401",
      call("/api/v1/system/status", client="127.0.0.1",
           headers=[(b"x-forwarded-for", b"1.2.3.4")])[0] == 401)

print("\n3. the cloud routes keep their own edge_id check")
for p, m in (("/api/v1/cameras/ptz", "POST"), ("/api/v1/cameras/control", "POST"),
             ("/api/v1/recordings/timeline", "GET"),
             ("/api/v1/recordings/LOUNGE/playback.m3u8", "GET"),
             ("/api/v1/recordings/LOUNGE/segment/a.mp4", "GET"),
             ("/api/v1/talkback/cameras", "GET"), ("/api/v1/talkback/LOUNGE/test", "POST")):
    check(f"cloud: {m} {p}", call(p, m)[0] == 200)
check("…but a UI-only camera route is still protected",
      call("/api/v1/cameras/LOUNGE/ptz", "POST")[0] == 401)

print("\n4. signing in")
st, h, _ = call("/api/v1/account/verify", "POST", answer={"verified": False})
check("verified: false -> no cookie", "set-cookie" not in h)
st, h, _ = call("/api/v1/account/verify", "POST",
                answer={"verified": True, "user": {"email": "a@b.c"}})
cookie = h.get("set-cookie", "")
check("verified: true -> a session cookie", cookie.startswith(auth.COOKIE + "="))
check("cookie is HttpOnly + SameSite=Strict, path /",
      "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/;" in cookie)
check("on the LAN it is not marked Secure (plain http)", "Secure" not in cookie)
tok = token_of(cookie)
check("the cookie opens a protected route", call("/api/v1/cameras", cookie=tok)[0] == 200)
check("a made-up token does not", call("/api/v1/cameras", cookie="x" * 43)[0] == 401)
st, h, _ = call("/api/v1/account/verify", "POST", fleet="EDGE42",
                answer={"verified": True, "user": {}})
fc = h.get("set-cookie", "")
check("through the tunnel: Secure, bound to /<edge_id>/",
      "Secure" in fc and "Path=/EDGE42/" in fc, fc)

print("\n5. idle limit")
settings.ui_session_idle_minutes = 1
check("fresh session passes", call("/api/v1/cameras", cookie=tok)[0] == 200)
digest = auth._digest(tok)
auth.sessions._sessions[digest]["active"] = time.time() - 50
for _ in range(3):
    call("/api/v1/cameras", cookie=tok)                   # polling
check("polling does not extend it",
      auth.sessions._sessions[digest]["active"] < time.time() - 45)
auth.sessions.touch(tok)                                    # a click
check("a user action does", auth.sessions._sessions[digest]["active"] > time.time() - 2)
auth.sessions._sessions[digest]["active"] = time.time() - 61
check("idle past the limit -> 401", call("/api/v1/cameras", cookie=tok)[0] == 401)
settings.ui_session_idle_minutes = 0
st, h, _ = call("/api/v1/account/verify", "POST", answer={"verified": True})
tok2 = token_of(h["set-cookie"])
auth.sessions._sessions[auth._digest(tok2)]["active"] = time.time() - 86400
check("limit 0 (dev) -> a day idle still signed in",
      call("/api/v1/cameras", cookie=tok2)[0] == 200)

print("\n6. sign-out, websockets, storage")
auth.sessions.revoke(tok2)
check("after sign-out the cookie is dead", call("/api/v1/cameras", cookie=tok2)[0] == 401)
_, _, code = call("/api/v1/some/socket", kind="websocket")
check("a websocket without a session is closed 4401", code == 4401)
raw = (Path(_TMP) / "ui_sessions.json").read_text()
check("the session file never holds a token", tok not in raw and tok2 not in raw)
if os.name == "posix":
    check("…and is owner-only",
          stat.S_IMODE((Path(_TMP) / "ui_sessions.json").stat().st_mode) == 0o600)
fresh = auth.SessionStore()
st, h, _ = call("/api/v1/account/verify", "POST", answer={"verified": True})
tok3 = token_of(h["set-cookie"])
check("sessions survive a restart (a new store reads them back)",
      fresh.check(tok3) is not None)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All API sign-in checks passed.")
