#!/usr/bin/env python3
"""
The edge login lets a person in ONLY when the app server confirms their code.

verify_otp used to accept any HTTP 2xx reply unless its body was exactly
{data:false}. The app server can report a failure INSIDE an HTTP 200 — the
Spring envelope {status:400, message:"Invalid OTP"}, {success:false}, … — so a
wrong code signed the person in. A partial code was sent as-is, the server's own
reason was replaced by "Invalid or expired code" (even for an API-key 401), and
the code was written in clear text to the wire log.

Checked, against a stand-in HTTP session (no network):
  1. in ONLY on the server's one confirmation — HTTP 200 {"code":1, …,
     "message":"OTP verified successfully"}. Out on anything else, with the
     server's message shown exactly as it sent it;
  2. /account/verify refuses a code that is not 5 digits WITHOUT asking the
     server, and turns "unreachable" / 5xx into a plain retry message;
  3. sendOtp: a refusal inside a 200 (code:0 "User not found", …) is
     reported, not "OTP sent";
  4. the wire log never holds the code;
  5. login.html never renders a server reason as HTML.

    python tests/test_login_otp.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-login-"))
os.environ["DATA_DIR"] = str(_TMP)           # wire log + account.json land here

import requests                                                   # noqa: E402

from config.settings import settings                              # noqa: E402
from integration import ceravis_api                               # noqa: E402
from api import account_routes                                    # noqa: E402

settings.ceravis_api_base_url = "http://app-server.test"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


class Reply:
    def __init__(self, status: int, body: str = "") -> None:
        self.status_code, self.text, self.headers = status, body, {}

    def json(self):
        return json.loads(self.text)             # ValueError on a non-JSON body


class Session:
    """Answers every POST with one canned reply (or raises), and counts calls."""

    def __init__(self) -> None:
        self.reply: Reply | Exception = Reply(200)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, **_):
        self.calls.append((url, json))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


SESSION = Session()
ceravis_api._session = lambda: SESSION


def answer(status: int, body: str = "") -> None:
    SESSION.reply, SESSION.calls = Reply(status, body), []


# --------------------------------------------------------------------------
OTP_OK = '{"code":1,"data":{"ceravisUserId":130,"deviceToken":"DMFpAL9PDnuAngA2n3cJ3XKZ0XeaPQI_UjD6ZvLerLo","email":"user@example.com","role":"CARE_RECIPIENT","tier":"TIER_3"},"message":"OTP verified successfully"}'

print("1. verify_otp: in ONLY on the server's confirmation; a refusal shows the server's message")
CASES = [
    # (label, HTTP status, body, accepted?, reason must contain)
    # -- the ONE confirmation (the app server's real reply, 2026-10-13): in --
    ("200 code:1 'OTP verified successfully'", 200, '__OK__', True, None),
    ("200 same, no account in data", 200, '{"code":1,"data":null,"message":"OTP verified successfully"}', True, None),
    ("200 code:'1', 'OTP Verified Successfully.'", 200, '{"code":"1","message":"OTP Verified Successfully."}', True, None),
    # -- the server's refusals: out, its message shown exactly --
    ("200 code:0 'No OTP found…'", 200, '{"code":0,"data":null,"message":"No OTP found. Please request OTP again"}', False, "No OTP found. Please request OTP again"),
    ("200 code:0 'Invalid OTP'", 200, '{"code":0,"data":null,"message":"Invalid OTP"}', False, "Invalid OTP"),
    ("200 code:0 'OTP expired'", 200, '{"code":0,"data":null,"message":"OTP expired"}', False, "OTP expired"),
    # -- anything short of the confirmation: out --
    ("200 code:0 + success words", 200, '{"code":0,"message":"OTP verified successfully"}', False, "did not confirm"),
    ("200 code:1, other message", 200, '{"code":1,"message":"OTP sent"}', False, "did not confirm"),
    ("200 code:1 + 'Invalid OTP'", 200, '{"code":1,"message":"Invalid OTP"}', False, "Invalid OTP"),
    ("200 message only, no code", 200, '{"message":"OTP verified successfully"}', False, "did not confirm"),
    ("200 'verified' inside a longer message", 200, '{"code":1,"message":"OTP verified successfully? No: expired"}', False, "No: expired"),
    ("200 {data:true}", 200, '{"data":true}', False, "did not confirm"),
    ("200 bare true", 200, "true", False, "did not confirm"),
    ("200 {success:true}", 200, '{"success":true}', False, "did not confirm"),
    ("200 plain text", 200, "OTP verified successfully", False, "did not confirm"),
    ("200 empty body", 200, "", False, "did not confirm"),
    ("204 no content", 204, "", False, "did not confirm"),
    ("200 deeply nested JSON (no crash)", 200, "[" * 100000 + "]" * 100000, False, "did not confirm"),
    # -- not 2xx: out --
    ("300 with the confirmation", 300, '__OK__', False, "Invalid or expired"),
    ("400 with message", 400, '{"message":"Invalid OTP"}', False, "Invalid OTP"),
    ("401 API key", 401, '{"message":"Invalid API key"}', False, "Invalid API key"),
    ("401 Spring default", 401, '{"status":401,"error":"Unauthorized"}', False, "refused this device"),
    ("404 Spring default", 404, '{"status":404,"error":"Not Found","path":"/x"}', False, "Invalid or expired"),
    ("429 no body", 429, "", False, "Wait a minute"),
    ("400 HTML page", 400, "<html><body>nope</body></html>", False, "Invalid or expired"),
]
CASES = [(l, st, OTP_OK if b == "__OK__" else b, a, w) for l, st, b, a, w in CASES]
for label, status, body, accepted, why in CASES:
    answer(status, body)
    ok, reason, account = ceravis_api.verify_otp("user@example.com", "12345")
    good = ok is accepted and (reason is None if accepted else bool(why) and why in (reason or ""))
    check(f"{label:36} -> {'in ' if ok else 'out'} {reason!r}", good and (ok or account is None))
answer(200, OTP_OK)
check("the confirmation hands back the account it confirmed (deviceToken = edge_id)",
      (ceravis_api.verify_otp("user@example.com", "12345")[2] or {}).get("deviceToken")
      == "DMFpAL9PDnuAngA2n3cJ3XKZ0XeaPQI_UjD6ZvLerLo")

for label, exc in (("unreachable", requests.ConnectionError("refused")),):
    SESSION.reply = exc
    try:
        ceravis_api.verify_otp("user@example.com", "12345")
        check(f"{label} raises CeravisApiError", False)
    except ceravis_api.CeravisApiError as e:
        check(f"{label} raises CeravisApiError (status None)", e.status is None)
answer(503, "<html>down</html>")
try:
    ceravis_api.verify_otp("user@example.com", "12345")
    check("503 raises CeravisApiError", False)
except ceravis_api.CeravisApiError as e:
    check("503 raises CeravisApiError (status 503)", e.status == 503)


# --------------------------------------------------------------------------
print("\n2. /account/verify: a 5-digit code or nothing; plain words on trouble")
Verify = account_routes.VerifyRequest
for bad in ("", "123", "123456", "12a45", "12 34", "\u0661\u0662\u0663\u0664\u0665"):
    answer(200, '{"data":true}')
    r = account_routes.verify(Verify(email="user@example.com", otp=bad), None, None)
    check(f"otp {bad!r:10} refused before the server is asked",
          r == {"verified": False, "reason": "Enter the 5-digit code from your email."}
          and not SESSION.calls, str(r))
answer(200, '{"status":400,"message":"Invalid OTP"}')
r = account_routes.verify(Verify(email="user@example.com", otp=" 12345 "), None, None)
check("server refusal passes its message through",
      r == {"verified": False, "reason": "Invalid OTP"} and len(SESSION.calls) == 1, str(r))
SESSION.reply = requests.Timeout("read timed out")
r = account_routes.verify(Verify(email="user@example.com", otp="12345"), None, None)
check("unreachable -> 'Couldn't reach the CERAVIS server…'",
      not r["verified"] and r["reason"].startswith("Couldn't reach the CERAVIS server"), str(r))
answer(502, "<html>bad gateway</html>")
r = account_routes.verify(Verify(email="user@example.com", otp="12345"), None, None)
check("5xx -> plain retry message, no HTML",
      not r["verified"] and "try again" in r["reason"] and "<" not in r["reason"], str(r))


# --------------------------------------------------------------------------
print("\n2b. the confirmed account decides the edge_id: same -> returning, new -> updated")
from types import SimpleNamespace                                 # noqa: E402
from fastapi import BackgroundTasks                               # noqa: E402


class _Account:                                    # account.json, kept in memory
    def __init__(self, data):
        self.data = dict(data)

    def get(self):
        return dict(self.data)

    def get_edge_id(self):
        return self.data.get("edgeId") or ""

    def save(self, account):
        self.data = dict(account)


def _login(token):
    """One sign-in whose verifyOtp confirmation carries `token` (None = no account)."""
    data = None if token is None else {
        "ceravisUserId": 5012, "deviceToken": token, "email": "user@example.com",
        "firstName": "Arnold ", "lastName": "Benner", "role": "CARE_RECIPIENT", "tier": "TIER_2"}
    answer(200, json.dumps({"code": 1, "data": data, "message": "OTP verified successfully"}))
    tasks = BackgroundTasks()
    r = account_routes.verify(Verify(email="user@example.com", otp="12345"),
                              SimpleNamespace(headers={"host": "edge.test"},
                                              app=SimpleNamespace(state=SimpleNamespace())), tasks)
    return r, [t.func.__name__ for t in tasks.tasks], [u.rsplit("/", 1)[-1] for u, _ in SESSION.calls]


_real_account = account_routes.account_config
STORED = {"ceravisUserId": 5012, "email": "user@example.com", "edgeId": "CAr5old"}
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login("CAr5old")
check("same recipient, SAME edge_id -> returning login, nothing re-applied, no restart",
      r["verified"] and r["fresh"] is False and r["edgeId"] == "CAr5old"
      and calls == ["verifyOtp"] and tasks == [], f"{r.get('fresh')} {tasks} {calls}")
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login("VzspNew")
check("same recipient, NEW edge_id (moved server) -> saved, applied, then the service restarts",
      r["verified"] and r["fresh"] is True and r["restarting"] is True
      and account_routes.account_config.get_edge_id() == "VzspNew"
      and tasks == ["apply_edge_id_async", "_repoint_live_paths", "_restart_after_switch"],
      f"{r.get('fresh')} {tasks}")
check("…and the reply carries this process's boot time for the page to wait on",
      r.get("bootedAt") == account_routes._BOOTED_AT
      and account_routes.get_account()["bootedAt"] == account_routes._BOOTED_AT)
check("…from the account the code confirmed — no second userDetails call",
      calls == ["verifyOtp"], str(calls))
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login(None)
check("a confirmation without an account keeps the returning login as before",
      r["verified"] and r["fresh"] is False and r["edgeId"] == "CAr5old", str(r.get("fresh")))
account_routes.account_config = _Account({})
r, tasks, calls = _login(None)
check("…and a new device without one falls back to userDetails",
      calls == ["verifyOtp", "userDetails"], str(calls))
account_routes.account_config = _Account({"ceravisUserId": 4000, "email": "other@example.com",
                                           "edgeId": "VzspNew"})
r, tasks, calls = _login("VzspNew")
check("a DIFFERENT recipient on the same edge_id still restarts (galleries, alerts follow)",
      r["fresh"] is True and r["restarting"] is True and tasks[-1] == "_restart_after_switch", str(tasks))
account_routes.account_config = _real_account

from maintenance import refresh as _refresh                       # noqa: E402
_signalled: list[bool] = []
_real_restart, _refresh.restart_service = _refresh.restart_service, lambda: _signalled.append(True) or True
account_routes._restart_after_switch()
_refresh.restart_service = _real_restart
check("the restart goes through the nightly refresh's own clean self-restart", _signalled == [True])
page = (ROOT / "edge/static/login.html").read_text(encoding="utf-8")
check("the login page waits for the NEW process, then opens the new edge_id address",
      "waitForRestart(prefix, r.bootedAt)" in page and "a.bootedAt !== bootedAt" in page
      and "`${prefix}/ui/${next}`" in page)


# --------------------------------------------------------------------------
print("\n3. sendOtp: a refusal inside HTTP 200 is not 'sent'; its wording is not judged")
Req = account_routes.RequestOtpRequest
for label, status, body, sent, why in (
        ("200 envelope OK", 200, '{"status":200,"message":"OTP sent","data":null}', True, None),
        ("200 'expires in 5 minutes'", 200, '{"status":200,"message":"OTP sent. It expires in 5 minutes"}', True, None),
        ("200 data.verified:false", 200, '{"status":200,"message":"OTP sent","data":{"verified":false}}', True, None),
        ("200 CERAVIS code:1 'OTP sent'", 200, '{"code":1,"data":{"email":"user@example.com","message":"2 More Send Otp Attempts!","userMemberId":"ME0056"},"message":"OTP sent"}', True, None),
        ("200 CERAVIS code:0 'User not found'", 200, '{"code":0,"data":null,"message":"User not found"}', False, "User not found"),
        ("200 {success:false}", 200, '{"success":false,"message":"Account locked"}', False, "Account locked"),
        ("200 status:'FAILED'", 200, '{"status":"FAILED","message":"Could not send email"}', False, "Could not send email"),
        ("200 'Success' + data:false", 200, '{"status":200,"message":"Success","data":false}', False, "did not send the code"),
        ("200 'OTP sent' + data:false", 200, '{"status":200,"message":"OTP sent","data":false}', False, "did not send the code"),
        ("200 status:'NOT_FOUND'", 200, '{"status":"NOT_FOUND","message":"No account exists for this email"}', False, "No account exists"),
        ("404 Spring default", 404, '{"status":404,"error":"Not Found"}', False, "No CERAVIS account found"),
        ("429 with message", 429, '{"message":"Too many codes requested"}', False, "Too many codes"),
        ("500 HTML", 500, "<html>x</html>", False, "try again")):
    answer(status, body)
    r = account_routes.request_otp(Req(email="user@example.com"))
    good = r.get("sent") is sent and (sent or why in (r.get("reason") or ""))
    check(f"{label:22} -> {r}", good)


# --------------------------------------------------------------------------
print("\n4. the wire log never holds the code")
answer(200, '{"data":true}')
ceravis_api.verify_otp("user@example.com", "97531")
wire = (_TMP / "ceravis_api_wire.jsonl").read_text(encoding="utf-8")
check("code '97531' absent from the wire log", "97531" not in wire)
check("verifyOtp records show it masked", '"otp": "*****"' in wire)
check("…while the server still received the real code",
      SESSION.calls and SESSION.calls[-1][1].get("otp") == "97531")


# --------------------------------------------------------------------------
print("\n5. login.html renders server reasons as text, never HTML")
page = (ROOT / "edge/static/login.html").read_text(encoding="utf-8")
html_with_reason = re.findall(r"innerHTML\s*=[^;]*reason", page)
check("no innerHTML built from a reason", not html_with_reason, str(html_with_reason))
check("Verify signs in only on verified === true", "r.verified !== true" in page)
check("the corner pop is built as text, never HTML", "${msg}" not in page)
check("a refused send stays on the email screen with the red pop",
      re.search(r"r\.sent !== true\) \{\s*lgFail\(", page) is not None)


print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All login OTP checks passed.")
