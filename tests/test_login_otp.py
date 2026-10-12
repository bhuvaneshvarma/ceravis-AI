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
  1. every reply shape: in ONLY on data:true, a bare true, or an OTP flag
     (otpVerified/otpValid) set true — with no refusal anywhere in the reply;
     a message, token or status word never confirms a code. Out otherwise,
     with the server's own words;
  2. /account/verify refuses a code that is not 5 digits WITHOUT asking the
     server, and turns "unreachable" / 5xx into a plain retry message;
  3. sendOtp: a refusal inside a 200 is reported, not "OTP sent";
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
print("1. verify_otp: in ONLY on data:true or an OTP flag set true; any refusal wins")
CASES = [
    # (label, HTTP status, body, accepted?, reason must contain)
    # -- the explicit confirmations: in --
    ("200 {data:true}", 200, '{"status":200,"message":"OTP verified","data":true}', True, None),
    ("200 bare true", 200, "true", True, None),
    ("201 {data:true}", 201, '{"data":true}', True, None),
    ("200 {otpVerified:true}", 200, '{"status":"SUCCESS","otpVerified":true}', True, None),
    ("200 snake data.otp_valid:true", 200, '{"success":true,"data":{"otp_valid":true}}', True, None),
    ("200 data.isOtpVerified + user", 200, '{"status":200,"data":{"isOtpVerified":true,"userId":7}}', True, None),
    ("200 data:true, Spring error:'OK'", 200, '{"status":200,"error":"OK","data":true}', True, None),
    ("200 data:true, 'Failed attempts reset'", 200, '{"data":true,"message":"OTP verified. Failed attempts reset."}', True, None),
    # -- explicit refusals: out, with the server's own words --
    ("200 {data:false} + message", 200, '{"status":200,"message":"Invalid OTP","data":false}', False, "Invalid OTP"),
    ("200 envelope status:400", 200, '{"status":400,"message":"Invalid OTP","data":null}', False, "Invalid OTP"),
    ("200 status:'FAILED'", 200, '{"status":"FAILED","message":"OTP does not match"}', False, "does not match"),
    ("200 status:'NOT_FOUND'", 200, '{"status":"NOT_FOUND","message":"No OTP for this email"}', False, "No OTP"),
    ("200 status:'400 BAD_REQUEST'", 200, '{"status":"400 BAD_REQUEST","message":"Bad OTP"}', False, "Bad OTP"),
    ("200 code:'OTP_MISMATCH' + data:true", 200, '{"code":"OTP_MISMATCH","message":"2 attempts left","data":true}', False, "2 attempts left"),
    ("200 responseCode:400 + otpVerified", 200, '{"responseCode":400,"responseMessage":"OTP expired","data":{"otpVerified":true}}', False, "OTP expired"),
    ("200 otpStatus:'EXPIRED'", 200, '{"otpVerified":true,"otpStatus":"EXPIRED"}', False, "Invalid or expired"),
    ("200 {success:false}", 200, '{"success":false,"message":"Wrong code"}', False, "Wrong code"),
    ("200 'Success' + data:false", 200, '{"status":200,"message":"Success","data":false}', False, "Invalid or expired"),
    ("200 {data:'false'}", 200, '{"status":200,"data":"false"}', False, "Invalid or expired"),
    ("200 data.verified:false", 200, '{"status":200,"data":{"verified":false}}', False, "Invalid or expired"),
    ("200 snake data.otp_verified:false", 200, '{"status":200,"data":{"otp_verified":false}}', False, "Invalid or expired"),
    ("200 data.success:false", 200, '{"status":200,"message":"Success","data":{"success":false}}', False, "Invalid or expired"),
    ("200 data.matched:false", 200, '{"success":true,"data":{"matched":false}}', False, "Invalid or expired"),
    ("200 response.otpVerified:false", 200, '{"status":200,"response":{"otpVerified":false}}', False, "Invalid or expired"),
    ("200 data:[{verified:false}]", 200, '{"status":"SUCCESS","data":[{"verified":false}]}', False, "Invalid or expired"),
    ("200 data:true but verified:'no'", 200, '{"data":true,"verified":"no"}', False, "Invalid or expired"),
    ("200 otpValid:true + expired:true", 200, '{"otpValid":true,"expired":true}', False, "Invalid or expired"),
    ("200 data:true + error object", 200, '{"data":true,"error":{"code":"OTP_INVALID","message":"Invalid OTP"}}', False, "Invalid or expired"),
    ("200 data:true + errors list", 200, '{"data":true,"errors":[{"message":"Invalid OTP"}]}', False, "Invalid or expired"),
    ("200 {errors:['otp: invalid']}", 200, '{"status":200,"errors":["otp: invalid"]}', False, "Invalid or expired"),
    ("200 data:true + 'Incorrect OTP'", 200, '{"data":true,"message":"Incorrect OTP"}', False, "Incorrect OTP"),
    ("200 data:true + 'OTP has expired'", 200, '{"data":true,"message":"OTP has expired"}', False, "OTP has expired"),
    ("200 otpVerified + 'OTP not valid'", 200, '{"data":{"otpVerified":true},"msg":"OTP not valid"}', False, "OTP not valid"),
    ("200 status:200 + 'Invalid OTP'", 200, '{"status":200,"message":"Invalid OTP","data":null}', False, "Invalid OTP"),
    ("200 duplicate keys data:false/true", 200, '{"data":false,"data":true}', False, "did not confirm"),
    ("200 nested 10 deep + data:true", 200, '{"data":true,"a":' + '{"b":' * 10 + '{}' + '}' * 10 + '}', False, "Invalid or expired"),
    # -- no confirmation: out ("did not confirm"), the server's words shown --
    ("200 generic {verified:true}", 200, '{"verified":true,"message":"Email already verified"}', False, "Email already verified"),
    ("200 status:'VERIFIED'", 200, '{"status":"VERIFIED","message":"OTP verified"}', False, "did not confirm"),
    ("200 data.accessToken", 200, '{"success":true,"data":{"accessToken":"t"}}', False, "did not confirm"),
    ("200 token 'Bearer null'", 200, '{"status":200,"message":"OTP check","data":{"token":"Bearer null"}}', False, "did not confirm"),
    ("200 plain-text JWT", 200, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2ln", False, "did not confirm"),
    ("200 plain text 'error.otp.invalid'", 200, "error.otp.invalid", False, "did not confirm"),
    ("200 'java.lang.IllegalStateException'", 200, "java.lang.IllegalStateException", False, "did not confirm"),
    ("200 success:true + 'Invalid OTP'", 200, '{"success":true,"message":"Invalid OTP"}', False, "Invalid OTP"),
    ("200 {code:200,msg:'Invalid OTP'}", 200, '{"code":200,"msg":"Invalid OTP"}', False, "Invalid OTP"),
    ("200 data:'INVALID_OTP'", 200, '{"status":200,"data":"INVALID_OTP"}', False, "did not confirm"),
    ("200 'OK, but the OTP has been used'", 200, '{"message":"OK, but the OTP has been used"}', False, "has been used"),
    ("200 message-only 'OTP verified'", 200, '{"status":200,"message":"OTP verified successfully"}', False, "it said: “OTP verified successfully”"),
    ("200 success:true only", 200, '{"success":true}', False, "did not confirm"),
    ("200 data:NaN", 200, '{"status":200,"data":NaN}', False, "did not confirm"),
    ("200 status:'\\u00b2' (no crash)", 200, '{"status":"\\u00b2","message":"x"}', False, "did not confirm"),
    ("200 deeply nested JSON (no crash)", 200, "[" * 100000 + "]" * 100000, False, "did not confirm"),
    ("200 empty body", 200, "", False, "did not confirm"),
    ("204 no content", 204, "", False, "did not confirm"),
    # -- not 2xx: out --
    ("300 {data:true}", 300, '{"data":true}', False, "Invalid or expired"),
    ("400 with message", 400, '{"message":"Invalid OTP"}', False, "Invalid OTP"),
    ("401 API key", 401, '{"message":"Invalid API key"}', False, "Invalid API key"),
    ("401 Spring default", 401, '{"status":401,"error":"Unauthorized"}', False, "refused this device"),
    ("404 Spring default", 404, '{"status":404,"error":"Not Found","path":"/x"}', False, "Invalid or expired"),
    ("410 expired", 410, '{"message":"OTP has expired"}', False, "OTP has expired"),
    ("429 with message", 429, '{"message":"Too many attempts"}', False, "Too many attempts"),
    ("429 no body", 429, "", False, "Wait a minute"),
    ("400 HTML page", 400, "<html><body>nope</body></html>", False, "Invalid or expired"),
]
for label, status, body, accepted, why in CASES:
    answer(status, body)
    ok, reason = ceravis_api.verify_otp("user@example.com", "12345")
    good = ok is accepted and (reason is None if accepted else bool(why) and why in (reason or ""))
    check(f"{label:36} -> {'in ' if ok else 'out'} {reason!r}", good)

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
print("\n3. sendOtp: a refusal inside HTTP 200 is not 'sent'; its wording is not judged")
Req = account_routes.RequestOtpRequest
for label, status, body, sent, why in (
        ("200 envelope OK", 200, '{"status":200,"message":"OTP sent","data":null}', True, None),
        ("200 'expires in 5 minutes'", 200, '{"status":200,"message":"OTP sent. It expires in 5 minutes"}', True, None),
        ("200 data.verified:false", 200, '{"status":200,"message":"OTP sent","data":{"verified":false}}', True, None),
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


print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All login OTP checks passed.")
