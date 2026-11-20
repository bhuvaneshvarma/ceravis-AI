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
  4. the code never leaves the device: verifyOtp sends no body, the wire log
     holds neither the code nor a key;
  5. login.html never renders a server reason as HTML;
  6. the signing itself, as fixed known answers (the values the Java app
     server must reproduce byte for byte);
  7. the whole signed sign-in over REAL HTTP against a stand-in app server
     that follows the same rules: the first sign-in signed with the API key;
     every sendOtp reply's randomId makes the next key from the current one,
     which replaces it only once the code checks out; a lost verifyOtp reply
     never locks the device out; a forged, unsigned or altered reply never
     acted on; a device that lost its key cannot fall back to the API key;
  8. removing the care recipient: a code to their email, then unassignDevice;
     nothing changes on the device unless both succeed; the device keeps its
     key, and the next care recipient signs in with it.

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
        self.content = body.encode("utf-8")

    def json(self):
        return json.loads(self.text)             # ValueError on a non-JSON body


class Session:
    """Answers every POST with one canned reply (or raises), and counts calls.
    Like the app server, it signs a 2xx reply with `key` over the request's
    timestamp — the device's own key, given below."""

    def __init__(self) -> None:
        self.reply: Reply | Exception = Reply(200)
        self.calls: list[tuple[str, dict | None]] = []      # (url, JSON body sent)
        self.sent: list[tuple[str, dict, bytes]] = []       # (url, headers, exact bytes)
        self.key = ""

    def post(self, url, data=b"", headers=None, **_):
        headers = dict(headers or {})
        self.calls.append((url, json.loads(data) if data else None))
        self.sent.append((url, headers, data))
        if isinstance(self.reply, Exception):
            raise self.reply
        reply = self.reply
        reply.headers = ({ceravis_api.HDR_SIGNATURE: ceravis_api._signature(
            self.key, headers[ceravis_api.HDR_TIMESTAMP], reply.content)}
            if 200 <= reply.status_code < 300 else {})
        return reply


SESSION = Session()
_real_session, ceravis_api._session = ceravis_api._session, lambda: SESSION
# Sections 1-5: a device whose first sign-in is long done — it holds its own key.
DEVICE_KEY = "5f1c0b7a" * 8
(_TMP / ceravis_api.EDGE_KEY_FILE).write_text(DEVICE_KEY, encoding="utf-8")
SESSION.key = DEVICE_KEY


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
print("\n2b. sign-in never decides the edge_id: the FMS issued it; the server's token is a hint")
from types import SimpleNamespace                                 # noqa: E402
from fastapi import BackgroundTasks                               # noqa: E402
from integration import call_log                                  # noqa: E402


class _Account:                                    # account.json, kept in memory
    def __init__(self, data):
        self.data = dict(data)

    def get(self):
        return dict(self.data)

    def save(self, account):
        self.data = dict(account)

    def clear(self):
        self.data = {}


def _login(token, reset_token=None):
    """One sign-in whose verifyOtp confirmation carries `token` (None = no account);
    with `reset_token`, the yes to a hand-over instead of a code."""
    data = None if token is None else {
        "ceravisUserId": 5012, "deviceToken": token, "email": "user@example.com",
        "firstName": "Arnold ", "lastName": "Benner", "role": "CARE_RECIPIENT", "tier": "TIER_2"}
    answer(200, json.dumps({"code": 1, "data": data, "message": "OTP verified successfully"}))
    tasks = BackgroundTasks()
    SESSION.calls.clear()
    r = account_routes.verify(Verify(email="user@example.com", otp="12345",
                                     reset_token=reset_token),
                              SimpleNamespace(headers={"host": "edge.test"},
                                              app=SimpleNamespace(state=SimpleNamespace())), tasks)
    return r, [t.func.__name__ for t in tasks.tasks], [u.rsplit("/", 1)[-1] for u, _ in SESSION.calls]


_real_account = account_routes.account_config
_real_record, noted = call_log.record, []
call_log.record = (lambda kind, ok, label="", error="", **_:       # the edge_id notes only
                   "edge_id" in (label or "") and noted.append((label, error)))
# get_account answers in full to this device itself, which proves it with the
# device's local key (api/auth, common/local_key).
from starlette.requests import Request as _Request                    # noqa: E402
from common.local_key import HEADER as _KEY_HEADER, local_key as _local_key  # noqa: E402
_LOCAL = _Request({"type": "http", "client": ("127.0.0.1", 1),
                   "headers": [(_KEY_HEADER.lower().encode(), _local_key.ensure().encode())]})

_real_edge_id, settings.edge_id = settings.edge_id, "ce7k2m9xq4w8n3"      # issued by the FMS
STORED = {"ceravisUserId": 5012, "email": "user@example.com", "edgeId": "CAr5old"}
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login("ce7k2m9xq4w8n3")
check("same recipient -> returning login, the FMS edge_id, nothing re-applied, no restart",
      r["verified"] and r["fresh"] is False and r["edgeId"] == "ce7k2m9xq4w8n3"
      and calls == ["verifyOtp", "userDetails"] and tasks == [] and not noted,
      f"{r.get('fresh')} {tasks} {calls}")
from common.device_identity import identity                       # noqa: E402
check("...and EVERY sign-in reports this device: userDetails {email, deviceId}",
      SESSION.calls[-1][1] == {"email": "user@example.com", "deviceId": identity().name},
      str(SESSION.calls[-1][1]))
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login("VzspNew")
check("same recipient, the server names the device differently -> STILL the FMS edge_id, no restart",
      r["fresh"] is False and r["edgeId"] == "ce7k2m9xq4w8n3" and tasks == []
      and account_routes.account_config.get() == STORED, f"{r.get('fresh')} {tasks}")
check("...and the mismatch is said plainly on the sync console, naming both",
      noted and "another edge_id" in noted[-1][0]
      and "VzspNew" in noted[-1][1] and "ce7k2m9xq4w8n3" in noted[-1][1], str(noted))
noted.clear()
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login(None)
check("a confirmation without an account keeps the returning login as before",
      r["verified"] and r["fresh"] is False and r["edgeId"] == "ce7k2m9xq4w8n3", str(r.get("fresh")))
account_routes.account_config = _Account({})
r, tasks, calls = _login("CAr5old")
saved = account_routes.account_config.get()
check("a new device: the account is saved — and holds NO edge_id (the FMS issues it)",
      r["fresh"] is True and saved.get("ceravisUserId") == 5012 and "edgeId" not in saved,
      str(saved))
check("...while the app server's other token for the device is said plainly, not kept",
      noted and "CAr5old" in noted[-1][1], str(noted))
check("...while the reply and every route use the FMS edge_id",
      r["edgeId"] == "ce7k2m9xq4w8n3", str(r.get("edgeId")))
check("...and the reply carries this process's boot time for the page to wait on",
      r.get("bootedAt") == account_routes._BOOTED_AT
      and account_routes.get_account(_LOCAL)["bootedAt"] == account_routes._BOOTED_AT)
account_routes.account_config = _Account({})
r, tasks, calls = _login(None)
check("...and a new device without one falls back to userDetails",
      calls == ["verifyOtp", "userDetails"], str(calls))
sent = SESSION.calls[-1][1]
check("...which sends the email AND this device's deviceId (its FMS device_id), nothing else",
      sent == {"email": "user@example.com", "deviceId": identity().name}
      and sent["deviceId"].startswith("ceravis-"), str(sent))
_real_details = account_routes.get_user_details


def _details_down(_email):
    raise ceravis_api.CeravisApiError("app server returned HTTP 503", status=503)


account_routes.get_user_details = _details_down
account_routes.account_config = _Account(STORED)
r, tasks, calls = _login("ce7k2m9xq4w8n3")
check("userDetails failing never blocks a returning sign-in; the stored account is untouched",
      r["verified"] and r["fresh"] is False and account_routes.account_config.get() == STORED, str(r))
account_routes.account_config = _Account({})
r, tasks, calls = _login("CAr5old")
check("...nor a first sign-in whose confirmation already carries the account",
      r["verified"] and r["fresh"] is True
      and account_routes.account_config.get().get("ceravisUserId") == 5012, str(r))
account_routes.account_config = _Account({})
r, tasks, calls = _login(None)
check("...but with NO account anywhere it still refuses, with the server's reason (as before)",
      not r["verified"] and "try again" in r["reason"], str(r))
account_routes.get_user_details = _real_details
from maintenance import recipient_reset                            # noqa: E402
OTHER = {"ceravisUserId": 4000, "email": "other@example.com", "edgeId": "VzspNew"}
account_routes.account_config = _Account(OTHER)
r, tasks, calls = _login("ce7k2m9xq4w8n3")
check("a DIFFERENT recipient is NOT let in at once: asked to confirm the hand-over",
      r["verified"] is False and r["confirmReset"] is True and r["resetToken"]
      and tasks == [] and account_routes.account_config.get() == OTHER
      and recipient_reset.pending() is None, str(r)[:200])
check("...the question says what is deleted and what stays, and names nobody",
      recipient_reset.ACCOUNT_LABEL in r["erased"] and r["kept"] == list(recipient_reset.KEPT)
      and "other@example.com" not in json.dumps(r) and "4000" not in json.dumps(r))
token = r["resetToken"]
r2, tasks, calls = _login(None, reset_token="not-the-token")
check("a wrong confirmation hands nothing over",
      r2["verified"] is False and account_routes.account_config.get() == OTHER
      and recipient_reset.pending() is None)
r2, tasks, calls = _login(None, reset_token=token)
saved = account_routes.account_config.get()
check("the yes hands the device over: new account, removal marked, restart queued",
      r2["verified"] is True and r2["fresh"] is True and r2["restarting"] is True
      and saved.get("ceravisUserId") == 5012 and recipient_reset.pending() is not None
      and tasks[-1] == "restart_soon" and r2["edgeId"] == "ce7k2m9xq4w8n3", str(tasks))
check("...without asking the server again (the code was already used)", calls == [], str(calls))
r3, tasks, calls = _login(None, reset_token=token)
check("...and that confirmation works once only", r3["verified"] is False)
(Path(settings.data_dir) / recipient_reset.MARKER).unlink()
account_routes.account_config = _Account({"ceravisUserId": 5012, "email": "old@example.com"})
r, tasks, calls = _login("ce7k2m9xq4w8n3")
check("the SAME recipient under a new email is no hand-over: no question, no restart",
      r["verified"] is True and r["restarting"] is False and recipient_reset.pending() is None)
settings.edge_id = ""
account_routes.account_config = _Account(STORED)
noted.clear()
r, tasks, calls = _login("CAr5old")
check("not enrolled with the fleet yet: sign-in still works, no edge_id, nothing to report",
      r["verified"] and r["edgeId"] is None and r["provisioning"] is False and not noted,
      f"{r.get('edgeId')} {noted}")
settings.edge_id = _real_edge_id
call_log.record = _real_record
account_routes.account_config = _real_account

from maintenance import refresh as _refresh                       # noqa: E402
_signalled: list[bool] = []
_real_restart, _refresh.restart_service = _refresh.restart_service, lambda: _signalled.append(True) or True
recipient_reset.restart_soon()
_refresh.restart_service = _real_restart
check("the restart goes through the nightly refresh's own clean self-restart", _signalled == [True])
page = (ROOT / "edge/static/login.html").read_text(encoding="utf-8")
shared = (ROOT / "edge/static/ceravis.js").read_text(encoding="utf-8")
check("the login page waits for the NEW process, then opens the new edge_id address",
      "cvWaitForRestart(prefix, r.bootedAt)" in page and "a.bootedAt !== bootedAt" in shared
      and "`${prefix}/ui/${next}`" in page)
check("the login page asks before a hand-over, with a typed yes, and sends the token back",
      "r.confirmReset" in page and 'typeToConfirm: "DELETE"' in page
      and "reset_token: r.resetToken" in page)


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
print("\n4. the code never leaves the device; the wire log holds no code and no key")
answer(200, '{"data":true}')
ceravis_api.verify_otp("user@example.com", "97531")
wire = (_TMP / "ceravis_api_wire.jsonl").read_text(encoding="utf-8")
url, headers, raw = SESSION.sent[-1]
check("verifyOtp sends no body at all", raw == b"" and "Content-Type" not in headers, repr(raw))
check("...and the code is in no header either",
      not any("97531" in str(v) for v in headers.values()), str(headers))
check("...only its signature over the code, with this device's key",
      headers[ceravis_api.HDR_SIGNATURE] == ceravis_api._signature(
          DEVICE_KEY, headers[ceravis_api.HDR_TIMESTAMP], b"97531"))
check("...and no API key, no device header: timestamp and signature only",
      "X-API-Key" not in headers and "X-Ceravis-Device" not in headers
      and set(headers) == {"Accept", ceravis_api.HDR_TIMESTAMP, ceravis_api.HDR_SIGNATURE},
      str(sorted(headers)))
_, sent_headers, _ = next(s for s in SESSION.sent if s[0].endswith("/userDetails"))
check("sendOtp / userDetails name the device only in the body (no device header)",
      "X-Ceravis-Device" not in sent_headers, str(sorted(sent_headers)))
check("code '97531' absent from the wire log", "97531" not in wire)
check("the device key absent from the wire log", DEVICE_KEY not in wire)


# --------------------------------------------------------------------------
print("\n5. login.html renders server reasons as text, never HTML")
page = (ROOT / "edge/static/login.html").read_text(encoding="utf-8")
html_with_reason = re.findall(r"innerHTML\s*=[^;]*reason", page)
check("no innerHTML built from a reason", not html_with_reason, str(html_with_reason))
check("Verify signs in only on verified === true", "r.verified !== true" in page)
check("the corner pop is built as text, never HTML", "${msg}" not in page)
check("a refused send stays on the email screen with the red pop",
      re.search(r"r\.sent !== true\) \{\s*lgFail\(", page) is not None)


# --------------------------------------------------------------------------
print("\n6. the signing, as fixed known answers (dummy keys) for the Java side")
KA_API_KEY = "test-api-key-not-a-real-one"
KA_RANDOM_ID = "ddb23f51-aa93-4220-afec-5568202ec1e4"
KA_TS = "1793361045.218"
KA_BODY = b'{"email":"care.recipient@example.com","deviceId":"ceravis-acg08mbv3"}'
KA_REPLY = (b'{"code":1,"data":{"email":"care.recipient@example.com","message":"2 More Send '
            b'Otp Attempts!","randomId":"ddb23f51-aa93-4220-afec-5568202ec1e4","userMemberId":'
            b'"CARERECIPIENT"},"message":"OTP sent"}')
KA_EDGE_KEY = "25797315d0eb01a23b57c60b19f4e2c4def9cf364ee2cf93d1fc6dcfe9784fbb"
for label, got, want in (
        ("the body is sent as compact UTF-8 JSON",
         ceravis_api._json_bytes({"email": "care.recipient@example.com",
                                  "deviceId": "ceravis-acg08mbv3"}), KA_BODY),
        ("sendOtp request, signed with the API key",
         ceravis_api._signature(KA_API_KEY, KA_TS, KA_BODY),
         "56PrKnc+6otOwx/kbneuLZAeMCOX4VzkVnhRshdX8qs="),
        ("sendOtp reply, signed with the API key over the request timestamp",
         ceravis_api._signature(KA_API_KEY, KA_TS, KA_REPLY),
         "I0ZR2f++iRCFj1YCeU3qH01EDyuZT6fkK6Xq7tpjHjA="),
        ("device key = HMAC-SHA256(API key, randomId), hex",
         ceravis_api._derive_edge_key(KA_API_KEY, KA_RANDOM_ID), KA_EDGE_KEY),
        ("verifyOtp, code 48213, signed with the device key",
         ceravis_api._signature(KA_EDGE_KEY, KA_TS, b"48213"),
         "3zi+sxOZym2XhJ/PHP5qHpEkCkQ3SHQTQ69ed8BJCsY="),
        ("userDetails request, signed with the device key",
         ceravis_api._signature(KA_EDGE_KEY, KA_TS, KA_BODY),
         "HeWnOkWH7CEqYx3e86hlFUohnU+TAvyWZViPtUDYMxs=")):
    check(f"{label}: {got if isinstance(got, str) else got.decode()}", got == want)
KA_RANDOM_ID_2 = "7c1e9a52-3b4d-4f60-9e2a-0d5b8c7f1a36"
KA_RENEWED = "591cdceffd5d77f26beb179d70361ad9287ab61816fc7ab99d0dc0140532c610"
KA_UNASSIGN_REPLY = b'{"code":1,"message":"Device unassigned","data":{"deviceId":"ceravis-acg08mbv3"}}'
for label, got, want in (
        ("renewed key = HMAC-SHA256(device key, the next randomId), hex",
         ceravis_api._derive_edge_key(KA_EDGE_KEY, KA_RANDOM_ID_2), KA_RENEWED),
        ("verifyOtp after it, code 60417, signed with the renewed key",
         ceravis_api._signature(KA_RENEWED, "1793362001.250", b"60417"),
         "2ei47GQade4NwUjCyZWD5ZC4N+arntUZrnsWxIugivs="),
        ("unassignDevice request, signed with the renewed key",
         ceravis_api._signature(KA_RENEWED, "1793362002.750", KA_BODY),
         "7DZ6ocBBvI9kRF+FX2Q6kdbq3AVbW/0tICEYMVqs9CE="),
        ("unassignDevice reply, signed with it over the request timestamp",
         ceravis_api._signature(KA_RENEWED, "1793362002.750", KA_UNASSIGN_REPLY),
         "dwjS4Y1PZ5Pm1KZ6iulmA1ec2ofpG/6AsQ710vtKeNc=")):
    check(f"{label}: {got}", got == want)
check("a signature is checked in full: one character off fails",
      not ceravis_api._signed_by(KA_EDGE_KEY, KA_TS, b"48213",
                                 "3zi+sxOZym2XhJ/PHP5qHpEkCkQ3SHQTQ69ed8BJCsZ=")
      and not ceravis_api._signed_by(KA_EDGE_KEY, KA_TS, b"48213", None)
      and ceravis_api._signed_by(KA_EDGE_KEY, KA_TS, b"48213",
                                 "3zi+sxOZym2XhJ/PHP5qHpEkCkQ3SHQTQ69ed8BJCsY="))


# --------------------------------------------------------------------------
print("\n7. the whole signed sign-in over real HTTP, against a stand-in app server")
import secrets                                                    # noqa: E402
import threading                                                  # noqa: E402
import time                                                       # noqa: E402
import uuid                                                       # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

API_KEY = "test-api-key-not-a-real-one"
ACCOUNT = {"ceravisUserId": 5012, "firstName": "Care", "lastName": "Recipient",
           "email": "care@example.com", "role": "CARE_RECIPIENT", "tier": "TIER_2",
           "deviceToken": "ce7k2m9xq4w8n3"}
NEXT = {"ceravisUserId": 6020, "firstName": "Next", "lastName": "Recipient",
        "email": "next@example.com", "role": "CARE_RECIPIENT", "tier": "TIER_1"}
USERS = {a["email"]: a for a in (ACCOUNT, NEXT)}


class AppServer:
    """The Java side of the signed calls, as specified: the reference the real
    server is held to. Signatures are checked over the raw bytes received; a
    request older or newer than 300 s, or one already used, is refused. No
    request names its device in a header: the body's deviceId does — verifyOtp
    names nothing, its code is found by the signature alone.

    The key rule: every sendOtp reply carries a new randomId, and the device's
    next key is HMAC(the key that verified that sendOtp, randomId). verifyOtp
    is signed with it; when the code checks out it becomes the device key and
    the device is bound to that user. The key before stays accepted until the
    device first signs with the new one, so a lost verifyOtp reply never locks
    a device out."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}        # deviceId -> its key (after a confirmed code)
        self.previous: dict[str, str] = {}    # deviceId -> the key before, until the new one is used
        self.patient: dict[str, int | None] = {}   # deviceId -> the user it is assigned to
        self.pending: dict[str, dict] = {}    # deviceId -> {email, otp, next_key}
        self.used: set[str] = set()
        self.calls: list[tuple[str, str]] = []   # (endpoint, which key / verdict)
        self.tamper = self.unsigned = self.fail_after_verify = self.unassign_refuses = False

    def _match_code(self, ts: str, sig: str):
        """verifyOtp carries no deviceId: the open code whose expected signature
        equals the one received — (deviceId, key) or None. The key is in the
        signature, so two devices' equal codes can never be confused."""
        for device, p in self.pending.items():
            if ceravis_api._signed_by(p["next_key"], ts, p["otp"].encode(), sig):
                return device, p["next_key"]
        return None

    def _key_for(self, device: str, ts: str, raw: bytes, sig: str, api_ok: bool):
        """(the key that verifies this request, its name), or (None, None)."""
        current, before = self.keys.get(device), self.previous.get(device)
        if current and ceravis_api._signed_by(current, ts, raw, sig):
            self.previous.pop(device, None)            # the new key is in use
            return current, "device key"
        if before and ceravis_api._signed_by(before, ts, raw, sig):
            return before, "previous key"
        if current is None and api_ok and ceravis_api._signed_by(API_KEY, ts, raw, sig):
            return API_KEY, "API key"
        return None, None

    def handle(self, endpoint: str, headers, raw: bytes):
        ts = headers.get(ceravis_api.HDR_TIMESTAMP) or ""
        sig = headers.get(ceravis_api.HDR_SIGNATURE) or ""
        if headers.get("X-API-Key"):
            return 400, None, {"code": 0, "message": "edge calls carry no API key"}
        if headers.get("X-Ceravis-Device"):
            return 400, None, {"code": 0, "message": "X-Ceravis-Device is not accepted"}
        try:
            fresh = abs(time.time() - float(ts)) <= 300
        except ValueError:
            fresh = False
        if not fresh or sig in self.used:
            return 401, None, {"code": 0, "message": "Request expired or already used"}
        if endpoint == "verifyOtp":
            if raw:
                return 400, None, {"code": 0, "message": "verifyOtp takes no body"}
            found = self._match_code(ts, sig)
            if found is None:                         # no device known: nothing to sign with
                self.calls.append((endpoint, "no open code matches"))
                return 401, None, {"code": 0, "data": None, "message": "Invalid OTP"}
            device, key = found
            self.used.add(sig)
            email = self.pending.pop(device)["email"]
            if self.keys.get(device) and self.keys[device] != key:
                self.previous[device] = self.keys[device]
            self.keys[device] = key                   # the device's key is (re)bound here
            self.patient[device] = USERS[email]["ceravisUserId"]
            self.calls.append((endpoint, "ok"))
            if self.fail_after_verify:                # committed, but the reply is lost
                return 503, None, {"code": 0, "message": "Service Unavailable"}
            return 200, key, {"code": 1, "data": USERS[email], "message": "OTP verified successfully"}
        try:
            body = json.loads(raw)
        except ValueError:
            return 400, None, {"code": 0, "message": "bad body"}
        device = body.get("deviceId") or ""
        if not device:
            return 400, None, {"code": 0, "message": "deviceId required"}
        key, which = self._key_for(device, ts, raw, sig, api_ok=endpoint == "sendOtp")
        if key is None:
            self.calls.append((endpoint, "signature does not match"))
            return 401, None, {"code": 0, "message": "Signature does not match"}
        self.used.add(sig)
        self.calls.append((endpoint, which))
        user = USERS.get(body.get("email"))
        if user is None:
            return 200, key, {"code": 0, "data": None, "message": "User not found"}
        if endpoint == "userDetails":
            return 200, key, {"code": 1, "data": user, "message": "User details"}
        if endpoint == "unassignDevice":
            if self.unassign_refuses or self.patient.get(device) not in (None, user["ceravisUserId"]):
                return 200, key, {"code": 0, "data": None, "message": "Not allowed for this device"}
            self.patient[device] = None
            return 200, key, {"code": 1, "message": "Device unassigned", "data": {"deviceId": device}}
        otp, random_id = f"{secrets.randbelow(100000):05d}", str(uuid.uuid4())
        self.pending[device] = {"email": body["email"], "otp": otp,
                                "next_key": ceravis_api._derive_edge_key(key, random_id)}
        return 200, key, {"code": 1, "message": "OTP sent",
                          "data": {"email": body["email"], "message": "2 More Send Otp Attempts!",
                                   "randomId": random_id, "userMemberId": "CARERECIPIENT"}}


APP = AppServer()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, key, reply = APP.handle(self.path.rsplit("/", 1)[-1], self.headers, raw)
        out = json.dumps(reply).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if key and not APP.unsigned:
            self.send_header(ceravis_api.HDR_SIGNATURE, ceravis_api._signature(
                key, self.headers.get(ceravis_api.HDR_TIMESTAMP), out))
        if APP.tamper:
            out = out.replace(b"OTP sent", b"OTP SENT")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_):
        pass


os.environ["NO_PROXY"] = "127.0.0.1,localhost"
SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
settings.ceravis_api_base_url = f"http://127.0.0.1:{SERVER.server_address[1]}/ch"
settings.ceravis_api_key = API_KEY
ceravis_api._session = _real_session
DEVICE = identity().name
KEY_FILE = _TMP / ceravis_api.EDGE_KEY_FILE
KEY_FILE.unlink()                                     # a brand-new device
account_routes.account_config = _Account({})
WIRE = _TMP / "ceravis_api_wire.jsonl"
WIRE.unlink()


def _verify(otp, email="care@example.com"):
    return account_routes.verify(Verify(email=email, otp=otp),
                                 SimpleNamespace(headers={"host": "edge.test"}), BackgroundTasks())


def _key():
    return KEY_FILE.read_text(encoding="utf-8")


r = account_routes.request_otp(Req(email="care@example.com"))
check("first sign-in: sendOtp signed with the API key, accepted",
      r == {"sent": True, "email": "care@example.com"} and APP.calls[-1] == ("sendOtp", "API key"),
      f"{r} {APP.calls[-1:]}")
first_key = APP.pending[DEVICE]["next_key"]
check("...the reply's randomId makes the device key — held, NOT saved before the code",
      not KEY_FILE.exists() and ceravis_api._pending_key("care@example.com") == first_key)
code = APP.pending[DEVICE]["otp"]
wrong = f"{(int(code) + 1) % 100000:05d}"
r = _verify(wrong)
check("a wrong code: no open code matches its signature -> 401 'Invalid OTP', shown as sent; no key saved",
      r == {"verified": False, "reason": "Invalid OTP"} and not KEY_FILE.exists()
      and APP.calls[-1] == ("verifyOtp", "no open code matches"), str(r))
APP.pending = {"ceravis-otherhome": {"email": "next@example.com", "otp": code,       # same code,
                                     "next_key": "another-device-key"},               # another device,
               **APP.pending}                                                         # tried FIRST
r = _verify(code)
other = APP.pending.pop("ceravis-otherhome", None)
check("the right code: signed in as a new device", r["verified"] is True and r["fresh"] is True,
      str(r)[:160])
check("...found by its signature alone, never confused with another device holding the SAME code",
      DEVICE in APP.keys and "ceravis-otherhome" not in APP.keys and other is not None)
check("...the device key is saved, exactly the one both sides made",
      _key() == first_key and APP.keys[DEVICE] == first_key)
if os.name == "posix":
    check("...readable by the service alone (0600)", (KEY_FILE.stat().st_mode & 0o777) == 0o600,
          oct(KEY_FILE.stat().st_mode & 0o777))
check("...and userDetails went out signed with it, and was accepted",
      APP.calls[-2:] == [("verifyOtp", "ok"), ("userDetails", "device key")], str(APP.calls[-3:]))
check("...the account from the confirmation is kept on the device",
      account_routes.account_config.get().get("ceravisUserId") == 5012)
wire = WIRE.read_text(encoding="utf-8")
records = [json.loads(line) for line in wire.splitlines()]
check("the wire log holds no randomId, no code, no key",
      _key() not in wire and API_KEY not in wire
      and code not in json.dumps([(x["request"], x["response"]) for x in records])
      and any('"randomId": "<withheld>"' in (x["response"] or "") for x in records))

r = account_routes.request_otp(Req(email="care@example.com"))
renewed = ceravis_api._pending_key("care@example.com")
check("a later sign-in: sendOtp signed with the device key; its randomId makes the NEXT key "
      "from the device key, held", r["sent"] is True and APP.calls[-1] == ("sendOtp", "device key")
      and renewed == APP.pending[DEVICE]["next_key"] and renewed != first_key
      and _key() == first_key, str(APP.calls[-1:]))
r = _verify(APP.pending[DEVICE]["otp"])
check("...verifyOtp signed with the next key; confirmed, it REPLACES the device key on both sides",
      r["verified"] is True and r["fresh"] is False and _key() == renewed
      and APP.keys[DEVICE] == renewed, str(r)[:160])
check("...and the very next call is signed with the new key",
      APP.calls[-1] == ("userDetails", "device key") and DEVICE not in APP.previous)

r = account_routes.request_otp(Req(email="care@example.com"))
lost = APP.pending[DEVICE]["next_key"]
APP.fail_after_verify = True
r2 = _verify(APP.pending[DEVICE]["otp"])
APP.fail_after_verify = False
check("a verifyOtp the server committed but whose reply was lost: the device keeps its key",
      r2["verified"] is False and _key() == renewed and APP.keys[DEVICE] == lost, str(r2))
r = account_routes.request_otp(Req(email="care@example.com"))
check("...and is not locked out: the server still takes the key before, until the new one is used",
      r["sent"] is True and APP.calls[-1] == ("sendOtp", "previous key"), f"{r} {APP.calls[-1:]}")
r = _verify(APP.pending[DEVICE]["otp"])
check("...the next sign-in renews the key from there, both sides alike",
      r["verified"] is True and _key() == APP.keys[DEVICE], str(r)[:120])

r = account_routes.request_otp(Req(email="jürgen.ß@example.com"))
check("a non-ASCII email is signed over its exact UTF-8 bytes (the server checks, then answers)",
      r == {"sent": False, "reason": "User not found"} and APP.calls[-1] == ("sendOtp", "device key"),
      f"{r} {APP.calls[-1:]}")

APP.tamper = True
r = account_routes.request_otp(Req(email="care@example.com"))
APP.tamper = False
check("a reply altered on the way is not acted on",
      r == {"sent": False, "reason": ceravis_api._UNVERIFIED}, str(r))
APP.unsigned = True
r = account_routes.request_otp(Req(email="care@example.com"))
APP.unsigned = False
held = ceravis_api._pending_key("care@example.com")
r1 = account_routes.request_otp(Req(email="care@example.com"))
key_before = _key()
APP.unsigned = True
r2 = _verify(APP.pending[DEVICE]["otp"])
APP.unsigned = False
check("an unsigned reply is not acted on — not to send (no key held from it), not to sign in "
      "(the key not renewed)",
      r["sent"] is False and held is None and r1["sent"] is True
      and r2 == {"verified": False, "reason": ceravis_api._UNVERIFIED} and _key() == key_before,
      f"{r} {r2}")

saved = _key()
KEY_FILE.unlink()                                     # the device lost its key
r = account_routes.request_otp(Req(email="care@example.com"))
check("a device that lost its key cannot fall back to the API key: the server refuses",
      r == {"sent": False, "reason": "Signature does not match"}
      and APP.calls[-1] == ("sendOtp", "signature does not match"), f"{r} {APP.calls[-1:]}")
calls_before = len(APP.calls)
ceravis_api._drop_pending()                           # a restart: no code requested since
r = _verify("12345")
check("...and with no key and no code requested, nothing is sent at all",
      r == {"verified": False, "reason": "Ask for a new code first, then enter it here."}
      and len(APP.calls) == calls_before, str(r))
APP.keys.pop(DEVICE)                                  # a person clears the device on the server
APP.previous.pop(DEVICE, None)
APP.tamper = True
r = account_routes.request_otp(Req(email="care@example.com"))
APP.tamper = False
check("a first sign-in whose reply was altered holds no key from it",
      r["sent"] is False and ceravis_api._pending_key("care@example.com") is None, str(r))
r = account_routes.request_otp(Req(email="care@example.com"))
r2 = _verify(APP.pending[DEVICE]["otp"])
check("...cleared on the server too, the device signs up again with the API key",
      r["sent"] is True and r2["verified"] is True and KEY_FILE.exists() and _key() != saved,
      f"{r} {str(r2)[:80]}")

url = settings.ceravis_api_base_url + "/v1/ai/userDetails"
body = ceravis_api._json_bytes({"email": "care@example.com", "deviceId": DEVICE})
ts = f"{time.time():.3f}"
hdr = {ceravis_api.HDR_TIMESTAMP: ts,
       ceravis_api.HDR_SIGNATURE: ceravis_api._signature(_key(), ts, body),
       "Content-Type": "application/json"}
first = requests.post(url, data=body, headers=hdr, timeout=5)
again = requests.post(url, data=body, headers=hdr, timeout=5)
stale_ts = f"{time.time() - 600:.3f}"
stale = requests.post(url, data=body, headers=dict(
    hdr, **{ceravis_api.HDR_TIMESTAMP: stale_ts, ceravis_api.HDR_SIGNATURE:
            ceravis_api._signature(_key(), stale_ts, body)}), timeout=5)
check("server side: a captured request sent again, or 10 minutes old, is refused",
      first.status_code == 200 and again.status_code == 401 and stale.status_code == 401,
      f"{first.status_code} {again.status_code} {stale.status_code}")

# --------------------------------------------------------------------------
print("\n8. removing the care recipient: a code, then unassignDevice, then the device lets go")
from maintenance import recipient_reset                           # noqa: E402

_real_reset_request = recipient_reset.request
marks: list[tuple] = []
recipient_reset.request = lambda reason, actor, earlier=None: marks.append((reason, earlier))
_SIGNED_IN = _Request({"type": "http", "client": ("10.0.0.5", 1), "headers": []})


def _remove(otp):
    tasks = BackgroundTasks()
    r = account_routes.remove_recipient(account_routes.RemoveRecipientRequest(otp=otp),
                                        _SIGNED_IN, tasks)
    return r, [t.func.__name__ for t in tasks.tasks]


check("the device serves care@example.com, assigned on the server too",
      account_routes.account_config.get().get("ceravisUserId") == 5012
      and APP.patient[DEVICE] == 5012)
calls_before = len(APP.calls)
r, tasks = _remove("12")
check("no 5-digit code: refused on the device, nothing sent",
      r["removed"] is False and "5-digit" in r["reason"] and len(APP.calls) == calls_before
      and not tasks, str(r))
r = account_routes.remove_recipient_code()
check("'Remove': a code goes to the care recipient's own email, signed with the device key",
      r == {"sent": True, "email": "care@example.com"} and APP.calls[-1] == ("sendOtp", "device key")
      and APP.pending[DEVICE]["email"] == "care@example.com", f"{r} {APP.calls[-1:]}")
code = APP.pending[DEVICE]["otp"]
key_before = _key()
r, tasks = _remove(f"{(int(code) + 1) % 100000:05d}")
check("a wrong code: nothing removed, nothing unassigned, the code still open",
      r == {"removed": False, "reason": "Invalid OTP", "codeUsed": False} and not tasks
      and account_routes.account_config.get().get("ceravisUserId") == 5012
      and APP.patient[DEVICE] == 5012 and not marks and _key() == key_before, str(r))
APP.unassign_refuses = True
r, tasks = _remove(code)
APP.unassign_refuses = False
check("the right code but the server will not unassign: NOTHING removed, a new code asked for",
      r["removed"] is False and r["codeUsed"] is True and not tasks and not marks
      and r["reason"].startswith("Not allowed for this device") and "Nothing was removed" in r["reason"]
      and account_routes.account_config.get().get("ceravisUserId") == 5012
      and APP.patient[DEVICE] == 5012, str(r))
check("...the confirmed code renewed the device key on both sides all the same",
      _key() != key_before and _key() == APP.keys[DEVICE])
account_routes.remove_recipient_code()
r, tasks = _remove(APP.pending[DEVICE]["otp"])
check("the right code: unassignDevice signed with the renewed key, the server lets the device go",
      r["removed"] is True and r["restarting"] is True
      and APP.calls[-2:] == [("verifyOtp", "ok"), ("unassignDevice", "device key")]
      and APP.patient[DEVICE] is None, f"{r} {APP.calls[-3:]}")
check("...the account leaves at once, the rest is marked for the restart (logs set apart "
      "under the earlier recipient), the restart queued",
      account_routes.account_config.get() == {} and tasks == ["restart_soon"]
      and marks and marks[-1][0] == "care recipient removed"
      and marks[-1][1].get("ceravisUserId") == 5012, f"{tasks} {marks}")
check("...and the device keeps its key: it is the device's, not the care recipient's",
      KEY_FILE.exists() and _key() == APP.keys[DEVICE])
r = account_routes.remove_recipient_code()
check("with no care recipient left, a removal code is not even asked for",
      r["sent"] is False and r["reason"] == account_routes._NO_RECIPIENT, str(r))

kept = _key()
r = account_routes.request_otp(Req(email="next@example.com"))
check("a fresh sign-in with a NEW email: sendOtp signed with the device's existing key",
      r["sent"] is True and APP.calls[-1] == ("sendOtp", "device key"), f"{r} {APP.calls[-1:]}")
check("...its randomId makes the next key from that existing key",
      ceravis_api._pending_key("next@example.com")
      == APP.pending[DEVICE]["next_key"] != kept)
r = _verify(APP.pending[DEVICE]["otp"], email="next@example.com")
check("...the code checks out: the new care recipient on the device AND assigned on the server, "
      "the key renewed on both sides",
      r["verified"] is True and r["fresh"] is True and r["user"]["ceravisUserId"] == 6020
      and APP.patient[DEVICE] == 6020 and _key() == APP.keys[DEVICE] != kept, str(r)[:160])
check("...and userDetails, signed with the renewed key, accepted",
      APP.calls[-1] == ("userDetails", "device key"))
recipient_reset.request = _real_reset_request
SERVER.shutdown()
account_routes.account_config = _real_account


print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All login OTP checks passed.")
