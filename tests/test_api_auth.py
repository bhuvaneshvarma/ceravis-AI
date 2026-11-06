"""
The API sign-in gate (edge/api/auth.py) — every rule it enforces, driven with
raw ASGI requests (no web server, no browser):

  * a protected route answers 401 without a session, a public route does not
  * this device calling itself passes only with the device's local key (bare
    loopback reads the health surface, nothing else); never through the tunnel
  * the session cookie is Secure on every HTTPS page (tunnel and LAN listener)
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
from common.local_key import HEADER as LOCAL_KEY_HEADER, local_key  # noqa: E402

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
KEY = [(LOCAL_KEY_HEADER.lower().encode(), local_key.ensure().encode())]
check("loopback WITH the device's local key -> allowed",
      call("/api/v1/cameras", client="127.0.0.1", headers=KEY)[0] == 200)
check("loopback without the key -> 401 (reaching 127.0.0.1 proves nothing)",
      call("/api/v1/cameras", client="127.0.0.1")[0] == 401)
check("loopback with a wrong key -> 401",
      call("/api/v1/cameras", client="127.0.0.1",
           headers=[(KEY[0][0], b"x" * 43)])[0] == 401)
check("loopback without the key may still READ the health surface (the fleet agent)",
      call("/api/v1/system/status", client="127.0.0.1")[0] == 200)
check("…and the pipeline metrics (measuring on the device)",
      call("/api/v1/metrics", client="127.0.0.1")[0] == 200
      and call("/api/v1/metrics/pipelines", client="127.0.0.1")[0] == 200)
check("…read only", call("/api/v1/system/status", "POST", client="127.0.0.1")[0] == 401)
check("the LAN never gets that read", call("/api/v1/system/status")[0] == 401)
check("loopback THROUGH the tunnel (/<edge_id> prefix) -> 401, key or not",
      call("/api/v1/system/status", client="127.0.0.1", fleet="E1", headers=KEY)[0] == 401)
check("loopback carrying X-Forwarded-For (the cloud proxy) -> 401",
      call("/api/v1/system/status", client="127.0.0.1",
           headers=[(b"x-forwarded-for", b"1.2.3.4")] + KEY)[0] == 401)
key_file = Path(_TMP) / "local.key"
check("the key lives in the data folder", key_file.read_text().strip() == KEY[0][1].decode())
if os.name == "posix":
    check("…owner-only (0600)", stat.S_IMODE(key_file.stat().st_mode) == 0o600)
check("it is kept across restarts", auth.local_key.ensure() == KEY[0][1].decode())

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
lan_tls = auth._cookie_header({"scheme": "https"}, "t", 60).decode()
check("on the LAN's HTTPS listener: Secure too", "Secure" in lan_tls and "Path=/;" in lan_tls)

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
grace = auth.PROMPT_SECS + auth.REPORT_SECS
auth.sessions._sessions[digest]["active"] = time.time() - 60 - grace + 5
check("past the limit but inside the 'still watching?' window -> still signed in",
      call("/api/v1/cameras", cookie=tok)[0] == 200)
check("…and the page is told it is time to ask",
      auth.remaining_secs(auth.sessions._sessions[digest]) == 0)
st = auth._state(auth.sessions._sessions[digest], False)
check("the page learns the question's length and the report interval",
      st["promptSecs"] == 15 and st["reportSecs"] == 30 and st["idleMinutes"] == 1)
auth.sessions._sessions[digest]["active"] = time.time() - 60 - grace - 1
check("no answer either -> 401", call("/api/v1/cameras", cookie=tok)[0] == 401)
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

print("\n7. UI_TESTING_OPEN (bench / test devices only)")
from api.camera_routes import list_cameras      # noqa: E402
from fastapi.encoders import jsonable_encoder   # noqa: E402
from schemas.cameras import Camera              # noqa: E402
from starlette.requests import Request          # noqa: E402
READS = ("/api/v1/cameras", "/api/v1/cameras/status", "/api/v1/system/status",
         "/api/v1/ai/state", "/api/v1/recordings/state", "/api/v1/cloud/activity")
check("off by default", settings.ui_testing_open is False)
check("off: the testing reads -> 401", all(call(p)[0] == 401 for p in READS))
settings.ui_testing_open = True
check("on: the testing reads open on the LAN", all(call(p)[0] == 200 for p in READS))
check("on: …and through the tunnel (/<edge_id> prefix)",
      all(call(p, client="127.0.0.1", fleet="E1")[0] == 200 for p in READS))
check("on: a change is still 401 (recording on/off, a camera edit)",
      call("/api/v1/recordings/toggle", "POST")[0] == 401
      and call("/api/v1/cameras", "POST")[0] == 401
      and call("/api/v1/cameras/LOUNGE", "PUT", fleet="E1")[0] == 401)
check("on: every other screen's data is still 401",
      all(call(p, fleet="E1")[0] == 401
          for p in ("/api/v1/events", "/api/v1/recipients", "/api/v1/cloud/outbox",
                    "/api/v1/system/settings", "/api/v1/cameras/LOUNGE")))
check("on: the account answer stays the reduced one (no email, no edge_id)",
      not auth.signed_in(Request({"type": "http", "headers": [],
                                  "client": ("192.168.0.20", 1), FLEET_EDGE_KEY: "E1"})))
cam = Camera(room_name="Lounge", camera_id="LOUNGE", camera_name="Lounge cam",
             rtsp_url="rtsp://admin:S3cret@10.42.0.250:554/s0",
             ai_rtsp_url="rtsp://admin:S3cret@10.42.0.250:554/s1",
             record_rtsp_url="rtsp://admin:S3cret@10.42.0.250:554/s0",
             onvif_username="admin", onvif_password="S3cret", ptz_supported=True)
import api.camera_routes as _cr                 # noqa: E402
_real = _cr.camera_config.get_all
_cr.camera_config.get_all = lambda: [cam]
anon = list_cameras(Request({"type": "http", "headers": [], "client": ("1.2.3.4", 1),
                             FLEET_EDGE_KEY: "E1"}))
wire = json.dumps(jsonable_encoder(anon))       # what FastAPI would send
check("on: the open camera list has no credential in it",
      "S3cret" not in wire and "rtsp" not in wire and "onvif" not in wire, wire)
check("on: …but what the consoles need is there",
      anon == [{"camera_id": "LOUNGE", "camera_name": "Lounge cam", "room_name": "Lounge",
                "device_label": "", "ptz_supported": True, "is_enabled": True}])
st, h, _ = call("/api/v1/account/verify", "POST", answer={"verified": True})
mine = list_cameras(Request({"type": "http", "client": ("192.168.0.20", 1),
                             "headers": [(b"cookie", f"{auth.COOKIE}={token_of(h['set-cookie'])}".encode())]}))
check("signed in: the full camera record, exactly as before", mine == [cam])
_cr.camera_config.get_all = _real
settings.ui_testing_open = False
check("off again: back to 401", all(call(p)[0] == 401 for p in READS))
seal = (ROOT / "setup" / "seal.sh").read_text(encoding="utf-8")
check("seal.sh strips it from the unit's drop-ins (no golden image ships it)",
      "sed -i '/UI_TESTING_OPEN/d' /etc/systemd/system/ceravis.service.d/*.conf" in seal)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All API sign-in checks passed.")
