from __future__ import annotations

"""
Thin client for the CERAVIS application server (Spring).

Only the calls the edge device needs. Everything goes through here so the
server address lives in ONE place (device config) and never in the browser.

Endpoint used during setup:
    POST {base}/v1/ai/userDetails   body {"email": "..."}
    -> ApiResponse<UserDetailsResponse>
       { ceravisUserId, firstName, lastName, email, gender, role, tier,
         deviceToken }
    deviceToken is this device's edge_id — the fleet routing token used as the
    first segment of every live link and in cloud/frpc.toml locations.
A user that exists in the server's DB is what "verifies" the operator; if the
email isn't found, onboarding is blocked.
"""

import base64
import json
import logging
import re
import threading
import time
from http import HTTPStatus

import requests

from config.settings import resolve_path, settings
from integration import call_log
from common import clock


logger = logging.getLogger("integration")


# ---- one pooled connection per thread --------------------------------
# Every call used to be a bare requests.put/post, which opens a NEW TCP
# connection and runs a FULL TLS handshake each time. On a home uplink to a
# remote server that is a few hundred ms of pure overhead on every upload, and
# under retry pressure it is a stream of handshakes against one host.
#
# A Session keeps the connection alive, so the second call onward is just the
# request. It is per-THREAD on purpose: requests.Session is not documented as
# thread-safe, and the senders each run on their own thread, so a thread-local
# gives each one its own pool with no shared mutable state and no lock.
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=2, pool_maxsize=4,
            max_retries=0)              # retries are the outbox's job, not urllib3's
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


def _timeout() -> tuple[float, float]:
    """(connect, read). Splitting them matters: an UNREACHABLE server should
    fail in a couple of seconds, not sit on the read budget — the difference
    between a fast failure that frees the sender and a stalled one that blocks
    whatever is queued behind it."""
    read = float(settings.ceravis_api_timeout_secs)
    return (min(3.05, read), read)

# ---------------------------------------------------------------------------
# Wire log — the single, full-fidelity record of every app-server hit.
#
# One file (data/ceravis_api_wire.jsonl) captures the EXACT request we sent and
# the EXACT response we got back for every call to app.ceravishealth.in, so the
# whole conversation is inspectable by hand. This is the forensic detail level;
# call_log (data/cloud_calls.jsonl) stays the one-line console summary. Both are
# best-effort: a logging hiccup never touches the call itself.
# ---------------------------------------------------------------------------
_WIRE_FILE = "ceravis_api_wire.jsonl"
_WIRE_LOCK = threading.Lock()
_WIRE_MAX_BYTES = 4 * 1024 * 1024       # rotate past this…
_WIRE_KEEP_LINES = 500                   # …down to the newest N records
# Request fields that are large base64 blobs — logged as a short marker, never
# verbatim, so the wire log stays readable and small.
# Big base64 fields to collapse to a size marker in the wire log. saveSnapshot no
# longer sends base64 (it uploads image/video as multipart files, logged as byte-
# size markers by the caller); uploadEmbeddingFile still base64-encodes fileBytes.
_WIRE_REDACT_KEYS = ("fileBytes",)


def _wire_redact(body):
    """A copy of a request body safe to persist: big base64 fields collapse to
    a short size marker; everything else is kept exactly as sent."""
    if not isinstance(body, dict):
        return body
    out = {}
    for k, v in body.items():
        if k in _WIRE_REDACT_KEYS and isinstance(v, str):
            out[k] = f"<{k}: {len(v)} base64 chars>"
        else:
            out[k] = v
    return out


# Who hears that the app server just answered a call with a 2xx — the proof the
# path to it is clear. The cloud outbox registers its kick() here, so ANY good
# answer (a recordingEvent, an upload, the status beat) sends what it kept.
_REACHABLE: list = []


def on_reachable(callback) -> None:
    if callback not in _REACHABLE:
        _REACHABLE.append(callback)


def _wire(endpoint: str, method: str, url: str, request, *,
          status: int | None = None, response: str | None = None,
          error: str | None = None, latency_ms: float | None = None) -> None:
    """Append one full request/response record to data/ceravis_api_wire.jsonl.
    Best-effort — any failure here is swallowed and never affects the call.
    Every call ends here, so this is also where a 2xx answer is announced."""
    if status is not None and 200 <= status < 300:
        for callback in list(_REACHABLE):
            try:
                callback()
            except Exception:
                logger.exception("on_reachable hook failed")
    try:
        base = resolve_path(settings.data_dir)
        base.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": clock.now_iso(),
            "endpoint": endpoint,
            "method": method,
            "url": url,
            "request": _wire_redact(request),
            "http_status": status,
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
            "response": response,
            "error": error,
        }
        f = base / _WIRE_FILE
        with _WIRE_LOCK:
            with f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            if f.stat().st_size > _WIRE_MAX_BYTES:
                lines = f.read_text(encoding="utf-8").splitlines()[-_WIRE_KEEP_LINES:]
                f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        logger.warning("wire dump for %s failed", endpoint)


# The app server's fixed CameraName enum — `room` must be one of these exactly.
CAMERA_ROOM_ENUM = ("KITCHEN", "BEDROOM", "LIVING_ROOM", "LOUNGE", "LIVE_FEED")


def room_to_enum(room_name: str) -> str:
    """Normalize a saved room label to the server's CameraName enum, e.g.
    'Living Room' -> 'LIVING_ROOM'. Anything that doesn't match a known room
    falls back to LIVE_FEED so the call never fails on an unexpected label."""
    key = re.sub(r"[^A-Z0-9]+", "_", (room_name or "").upper()).strip("_")
    return key if key in CAMERA_ROOM_ENUM else "LIVE_FEED"


class CeravisApiError(Exception):
    """Transport / configuration / server error talking to the app server.

    `status` is the HTTP status when the server answered, and None when it never
    did (DNS, refused, timeout, TLS — i.e. the network is down). That one
    distinction is what lets the outbox tell "try again when the link is back"
    from "the server rejected this and always will"."""

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        # Seconds the server asked us to wait (Retry-After), if it said.
        self.retry_after = retry_after


def _brief(text: str | None, n: int) -> str:
    """A response body for a log line / error: whitespace collapsed, clipped.
    A proxy's 503 is a multi-line HTML page, which used to spill one journal
    line per HTML line (the full body stays in the wire log)."""
    return " ".join((text or "").split())[:n]


def _retry_after(resp) -> float | None:
    """The Retry-After header in seconds (delta form), capped at 5 minutes."""
    try:
        v = float(resp.headers.get("Retry-After", ""))
        return max(0.0, min(v, 300.0))
    except (TypeError, ValueError):
        return None


def is_configured() -> bool:
    return bool(settings.ceravis_api_base_url.strip())


def api_key_problem() -> str | None:
    """Why the app server refuses (or soon will refuse) this device's calls for
    want of its key, or None. The key is a secret in a root-only file that
    ceravis.service hands the app as a credential (/etc/ceravis/credstore, on
    a development clone and a production release alike); without it the device
    still runs and keeps every upload queued, so nothing fails on its own — this
    is what makes the gap loud: logged at boot (bootstrap.pipeline) and listed
    in /api/v1/system/status problems."""
    if not is_configured():
        return None
    key = settings.ceravis_api_key.strip()
    fix = "run setup/install_api_key.sh, then restart ceravis"
    if not key:
        return ("app-server API key not installed — the app server refuses this "
                f"device's alerts and snapshots (queued, not lost); {fix}")
    if key == type(settings).model_fields["ceravis_api_key"].default:
        return ("app-server API key is the old built-in one (public, to be "
                f"revoked) — {fix}")
    return None


def _with_key(headers: dict[str, str], idempotency_key: str | None) -> dict[str, str]:
    if idempotency_key:
        headers = dict(headers, **{"Idempotency-Key": idempotency_key})
    return headers


def _headers() -> dict[str, str]:
    # Every app-server call carries the API key in the "X-API-Key" header.
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    key = settings.ceravis_api_key.strip()
    if key:
        h["X-API-Key"] = key
    return h


def _unwrap(body):
    """Spring returns ApiResponse<T> = {status, message, data}. Return the inner
    payload if that envelope is present, else the body as-is."""
    if isinstance(body, dict) and "data" in body and set(body) <= {
            "status", "message", "data", "success", "code", "timestamp"}:
        return body.get("data")
    return body


def _json_or_none(resp):
    try:
        return resp.json()
    except (ValueError, RecursionError):
        return None


_HTTP_PHRASES = {s.phrase.lower() for s in HTTPStatus}


def _server_message(resp) -> str | None:
    """The human-readable reason an app-server reply gives, if any: the JSON
    `message` (or error/detail), else a short plain-text body. Never an HTML
    page or unreadable JSON (those stay in the wire log) and never a bare HTTP
    phrase such as Spring's default "Not Found", so the caller's own wording
    takes over."""
    body = _json_or_none(resp)
    if isinstance(body, dict):
        texts = [body.get(k) for k in ("message", "msg", "responseMessage", "error_description",
                                       "detail", "reason", "error")]
    elif isinstance(body, str):          # a JSON string
        texts = [body]
    elif body is None and not (resp.text or "").lstrip().startswith(("{", "[")):
        texts = [resp.text]              # plain text, not broken JSON
    else:
        texts = []
    for text in texts:
        text = text.strip() if isinstance(text, str) else ""
        if text and not text.startswith("<") and text.lower() not in _HTTP_PHRASES:
            return _brief(text, 200)
    return None


# Envelope readers. The app server answers with HTTP 200 and its own verdict
# inside the body — {"code":1|0, "data":…, "message":…} — so a 2xx status
# alone never says a call worked.
_STATUS_OK = {"SUCCESS", "SUCCEEDED", "TRUE"}
_STATUS_BAD = {"FAIL", "FAILED", "FAILURE", "ERROR", "INVALID", "REJECTED", "EXPIRED", "FALSE"}
_FAILED_STATUS = re.compile(r"FAIL|INVALID|MISMATCH|EXPIRED|ERROR|DENIED|REJECT|LOCKED|BLOCKED|"
                            r"WRONG|UNVERIFIED|UNAUTHORI|NOT_?(?:VERIFIED|VALID|FOUND|MATCH)", re.I)


def _key(name) -> str:
    """otp_verified, OTPVerified and otpVerified are one field."""
    return re.sub(r"[\s_-]", "", str(name)).casefold()


def _flag(value) -> bool | None:
    """A yes/no as servers send it: true/false, "true"/"false", "yes"/"no", 1/0."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if isinstance(value, (str, int)) else ""
    return True if text in ("true", "1", "yes", "y") else False if text in ("false", "0", "no", "n") else None


def _status_ok(status) -> bool | None:
    """An envelope status — 200, "400 BAD_REQUEST", "NOT_FOUND", "SUCCESS", … —
    as True (a success status), False (an error status) or None (unknown)."""
    text = str(status).strip().upper() if status is not None else ""
    number = re.match(r"([0-9]{3})(?![0-9])", text)
    named = HTTPStatus.__members__.get(text.replace(" ", "_"))
    code = int(number.group(1)) if number else named.value if named else None
    if code is not None:
        return True if 200 <= code < 300 else False if code >= 400 else None
    return True if text in _STATUS_OK else False if text in _STATUS_BAD else None


def _status_failed(value) -> bool:
    return (_status_ok(value) is False
            or (isinstance(value, str) and bool(_FAILED_STATUS.search(value))))


def _code_refused(top: dict) -> bool:
    """The app server's own envelope verdict: `code` 1 = done, 0 = refused —
    {"code":0,"data":null,"message":"User not found"} arrives as HTTP 200.
    An HTTP-style code (200, 400) is left to _status_failed."""
    return _flag(top.get("code")) is False


# verifyOtp's contract: ONE reply means the code is right —
#   HTTP 200 {"code":1, "data":{…the account…}, "message":"OTP verified successfully"}
# Every refusal is also HTTP 200, with its own message ("No OTP found. Please
# request OTP again", "Invalid OTP", …), and that message is shown as sent.
_OTP_VERIFIED = "otp verified successfully"


def _otp_confirmed(body) -> bool:
    """True only for the server's one confirmation: code 1 AND the message
    "OTP verified successfully" (any case, a trailing full stop allowed)."""
    return (isinstance(body, dict) and _flag(body.get("code")) is True
            and isinstance(body.get("message"), str)
            and body["message"].strip().rstrip(".!").casefold() == _OTP_VERIFIED)


def _send_refused(body) -> bool:
    """A 2xx sendOtp reply that says no code went out: code:0 ("User not
    found"), success:false, data:false or a failing status. Sending is not the
    gate — the code check is — so anything else counts as sent."""
    if isinstance(body, bool):
        return not body
    if not isinstance(body, dict):
        return False
    top = {_key(k): v for k, v in body.items()}
    return bool(_code_refused(top) or _flag(top.get("success")) is False or top.get("data") is False
                or any(_status_failed(v) for k, v in top.items() if k.endswith(("status", "code"))))


# A refusal whose own message is only a generic "it worked" is explained instead.
_GENERIC_OK = re.compile(
    r"^\W*(?:ok|done|success(?:ful(?:ly)?)?|otp (?:sent|verified)(?: successfully)?)\W*$", re.I)


def get_user_details(email: str) -> dict | None:
    """
    POST /v1/ai/userDetails. Returns the UserDetailsResponse dict if the account
    exists, None if no such user. Raises CeravisApiError on config/transport/
    server errors (so the caller can show a real reason).
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/userDetails"
    request_body = {"email": email}
    logger.info("userDetails -> POST %s  email=%s", url, email)
    t0 = time.perf_counter()
    try:
        resp = _session().post(url, json=request_body, headers=_headers(),
                             timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("userDetails: cannot reach %s — %s", url, exc)
        call_log.record("userDetails", False, label=email, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("userDetails", "POST", url, request_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc

    lat = (time.perf_counter() - t0) * 1000
    logger.info("userDetails <- HTTP %s", resp.status_code)
    call_log.record("userDetails", resp.status_code < 400, label=email,
                    status=resp.status_code, latency_ms=lat)
    _wire("userDetails", "POST", url, request_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning("userDetails error body: %s", _brief(resp.text, 300))
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}",
            status=resp.status_code)
    try:
        data = _unwrap(resp.json())
    except ValueError as exc:
        raise CeravisApiError("app server returned non-JSON",
                              status=resp.status_code) from exc
    if not isinstance(data, dict) or not data.get("ceravisUserId"):
        return None                              # account not found / empty
    return data


def send_otp(email: str) -> tuple[bool, str | None]:
    """
    POST /v1/ai/sendOtp — ask the app server to email a login one-time code.
    Body: {"email": "..."}. Carries the X-API-Key header like every /v1/ai call.

    Returns (True, None) when the server takes it, (False, reason) when it
    refuses — a 4xx, or a 2xx whose body says it failed — with the server's own
    message as the reason when it gives one. Raises CeravisApiError on config/
    transport/5xx errors. The full request+response is written to the wire log
    (data/ceravis_api_wire.jsonl) so the exchange is inspectable by hand.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/sendOtp"
    request_body = {"email": email}
    logger.info("sendOtp -> POST %s  email=%s", url, email)
    t0 = time.perf_counter()
    try:
        resp = _session().post(url, json=request_body, headers=_headers(),
                             timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("sendOtp: cannot reach %s — %s", url, exc)
        call_log.record("sendOtp", False, label=email, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("sendOtp", "POST", url, request_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("sendOtp <- HTTP %s", resp.status_code)
    _wire("sendOtp", "POST", url, request_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 500:
        call_log.record("sendOtp", False, label=email, status=resp.status_code,
                        latency_ms=lat)
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    message = _server_message(resp)
    reason = None
    if resp.status_code == 404:
        reason = message or "No CERAVIS account found for this email"
    elif not 200 <= resp.status_code < 300:
        reason = message or f"The CERAVIS server refused the request (HTTP {resp.status_code})"
    elif _send_refused(_json_or_none(resp)):
        reason = ((message if message and not _GENERIC_OK.match(message) else None)
                  or "The CERAVIS server did not send the code")
    call_log.record("sendOtp", reason is None, label=email, error=reason,
                    status=resp.status_code, latency_ms=lat)
    return reason is None, reason


# What the login screen says when a refused code's reply gives no reason itself.
_OTP_REFUSED = {
    401: "The CERAVIS server refused this device's request (HTTP 401)",
    403: "The CERAVIS server refused this device's request (HTTP 403)",
    429: "Too many attempts. Wait a minute, then try again.",
}


def verify_otp(email: str, otp: str) -> tuple[bool, str | None, dict | None]:
    """
    POST /v1/ai/verifyOtp — check the login one-time code.
    Body: {"email": "...", "otp": "..."}. X-API-Key header as usual.

    Fails CLOSED: returns (True, None, account) only for the server's one
    confirmation (_otp_confirmed: HTTP 2xx, code 1, "OTP verified successfully"),
    `account` being the user it confirmed — its `data`: ceravisUserId,
    deviceToken (this device's edge_id), names, role, tier — or None when the
    reply carries none. Any other reply gives (False, reason, None), the reason
    being the server's message exactly as sent ("No OTP found…", …).
    Raises CeravisApiError on config/transport/5xx errors. The full request +
    response is captured in the wire log, the code itself masked — that record
    shows the exact reply shape if a correct code is ever refused.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/verifyOtp"
    request_body = {"email": email, "otp": otp}
    log_body = {"email": email, "otp": "*" * len(otp)}
    logger.info("verifyOtp -> POST %s  email=%s", url, email)
    t0 = time.perf_counter()
    try:
        resp = _session().post(url, json=request_body, headers=_headers(),
                             timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("verifyOtp: cannot reach %s — %s", url, exc)
        call_log.record("verifyOtp", False, label=email, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("verifyOtp", "POST", url, log_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("verifyOtp <- HTTP %s", resp.status_code)
    _wire("verifyOtp", "POST", url, log_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 500:
        call_log.record("verifyOtp", False, label=email, status=resp.status_code,
                        latency_ms=lat)
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    body = _json_or_none(resp) if 200 <= resp.status_code < 300 else None
    ok = _otp_confirmed(body)
    account = body.get("data") if ok and isinstance(body.get("data"), dict) else None
    reason = None
    if not ok:
        message = _server_message(resp)
        reason = ((message if message and not _GENERIC_OK.match(message) else None)
                  or _OTP_REFUSED.get(resp.status_code)
                  or ("The CERAVIS server did not confirm the code. Please try again."
                      if resp.status_code < 300 else "Invalid or expired code"))
    call_log.record("verifyOtp", ok, label=email, error=reason,
                    status=resp.status_code, latency_ms=lat)
    return ok, reason, account


def save_cameras(patient_user_id, cameras: list[dict]):
    """
    PUT /v1/ai/saveCamera — register this patient's cameras with the app server.

    Body:
        { "patientUserId": <id>,
          "cameras": [ { device, model, supplier, room, url, rtspUrl,
                         recordRtspUrl, hlsUrl, onvifXaddr, onvifUsername,
                         onvifPassword, onvifProfileToken, onvifPtzToken,
                         ptzSupported, isEnabled, webrtcUrl }, ... ] }
    Each camera dict carries every contract key in the server's camelCase shape
    (built by account_routes._cloud_camera), with only device, model, supplier,
    room, ptzSupported and isEnabled filled — addresses, credentials and the
    live link stay on the device and go out empty. Returns the server's
    response payload; raises CeravisApiError on failure.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/saveCamera"
    payload = {"patientUserId": patient_user_id, "cameras": cameras}
    label = f"{len(cameras)} camera(s): " + \
        ", ".join(c.get("room", "?") for c in cameras)
    logger.info("saveCamera -> PUT %s  patient=%s  cameras=%d  rooms=%s",
                url, patient_user_id, len(cameras), [c.get("room") for c in cameras])
    t0 = time.perf_counter()
    try:
        resp = _session().put(url, json=payload, headers=_headers(),
                            timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("saveCamera: cannot reach %s — %s", url, exc)
        call_log.record("saveCamera", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("saveCamera", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveCamera <- HTTP %s  body=%s", resp.status_code, _brief(resp.text, 300))
    call_log.record("saveCamera", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    _wire("saveCamera", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True


def delete_camera(patient_user_id, room: str):
    """
    PUT /v1/ai/cameras/delete — remove ONE camera from this patient's account on
    the app server, mirroring the device's local delete so the cloud never keeps
    a camera the edge no longer has. Body: { "userId": <id>, "room": <CameraName
    enum, e.g. LIVING_ROOM> }. Returns the server response; raises CeravisApiError
    on failure (the caller treats it as best-effort so a cloud hiccup never blocks
    the local delete).
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/cameras/delete"
    payload = {"userId": patient_user_id, "room": room}
    label = f"delete camera: {room}"
    logger.info("cameras/delete -> PUT %s  user=%s  room=%s",
                url, patient_user_id, room)
    t0 = time.perf_counter()
    try:
        resp = _session().put(url, json=payload, headers=_headers(),
                            timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("cameras/delete: cannot reach %s — %s", url, exc)
        call_log.record("cameraDelete", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("cameraDelete", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("cameras/delete <- HTTP %s  body=%s", resp.status_code, _brief(resp.text, 200))
    call_log.record("cameraDelete", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    _wire("cameraDelete", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True


def delete_patient_zoning_file(user_id):
    """
    PUT /v1/ai/deletePatientZoningFile — remove this patient's existing ZONING
    file from cloud storage (S3) so a re-save writes a clean object instead of
    leaving a stale one behind (S3 objects can't be edited in place). Body:
    { "userId": <id> }. Called right before uploadEmbeddingFile("ZONING", …) so
    the cloud ends up with exactly one, current zones file per patient.

    A 404 means there was no prior file — that's a success for delete-then-write,
    so it returns True rather than raising. Returns the server response otherwise;
    raises CeravisApiError on a real failure. Callers treat it as best-effort:
    the delete is cleanup, and must never block saving the new zones file.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/deletePatientZoningFile"
    payload = {"userId": user_id}
    label = f"delete zoning file: user {user_id}"
    logger.info("deletePatientZoningFile -> PUT %s  user=%s", url, user_id)
    t0 = time.perf_counter()
    try:
        resp = _session().put(url, json=payload, headers=_headers(),
                            timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("deletePatientZoningFile: cannot reach %s — %s", url, exc)
        call_log.record("deleteZoningFile", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("deletePatientZoningFile", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("deletePatientZoningFile <- HTTP %s  body=%s",
                resp.status_code, _brief(resp.text, 200))
    call_log.record("deleteZoningFile", resp.status_code < 400 or resp.status_code == 404,
                    label=label, status=resp.status_code, latency_ms=lat)
    _wire("deletePatientZoningFile", "PUT", url, payload,
          status=resp.status_code, response=resp.text, latency_ms=lat)
    if resp.status_code == 404:
        return True                       # nothing stored yet — fine to write
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True


def alert_id_of(response) -> int | None:
    """Pull the created alert's id out of a saveAlert response so it can be
    linked onto the snapshots that belong to it. Tolerant of shape: the
    unwrapped payload may be the id itself (a bare Long) or an object carrying
    it under alertId / alertID / id. Returns None if no id is present."""
    def _as_long(v) -> int | None:
        if isinstance(v, bool):          # bool is an int subclass — exclude it
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v)
        return None

    if isinstance(response, dict):
        for key in ("alertId", "alertID", "id"):
            got = _as_long(response.get(key))
            if got is not None:
                return got
        return None
    return _as_long(response)


def save_alert(patient_user_id, alert_type: str, message_text: str, *,
               idempotency_key: str | None = None):
    """
    PUT /v1/ai/saveAlert — push one alert to the app server.
    Body: { patientUserId, alertType (e.g. "FALL"), messageText }.
    Returns the server's response payload (carries the new alert's id — see
    alert_id_of); raises CeravisApiError on failure.
    `idempotency_key` (the outbox job id) goes out as the Idempotency-Key
    header, identical on every retry of the same alert, so a server that
    honours it can drop the duplicate a timed-out-but-committed try leaves.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/saveAlert"
    payload = {"patientUserId": patient_user_id,
               "alertType": alert_type, "messageText": message_text}
    label = f"{alert_type} · {message_text}"
    logger.info("saveAlert -> PUT %s  patient=%s  type=%s", url,
                patient_user_id, alert_type)
    t0 = time.perf_counter()
    try:
        resp = _session().put(url, json=payload,
                              headers=_with_key(_headers(), idempotency_key),
                              timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("saveAlert: cannot reach %s — %s", url, exc)
        call_log.record("saveAlert", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("saveAlert", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveAlert <- HTTP %s  body=%s", resp.status_code, _brief(resp.text, 300))
    _wire("saveAlert", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        call_log.record("saveAlert", False, label=label,
                        status=resp.status_code, latency_ms=lat,
                        error=_brief(resp.text, 200))
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code, retry_after=_retry_after(resp))
    try:
        result = _unwrap(resp.json())
    except ValueError:
        result = True
    call_log.record("saveAlert", True, label=label, status=resp.status_code,
                    latency_ms=lat, alert_id=alert_id_of(result))
    return result


def send_recording_event(payload: dict) -> None:
    """
    POST /v1/ai/recordings/event — tell the app server that ONE camera has
    started, or finished, recording a stretch of footage.

    Body is the backend's TimelineSegmentEvent, key names verbatim (note the
    deliberate camelCase/snake_case mix — that is what the Java DTO declares):

        { "edgeId": "<edge_id>", "camera_id": "LIVING_ROOM",
          "status": "started" | "finalized",
          "segment": { "start": ISO-8601, "end": ISO-8601|null,
                       "seconds": float|null } }

    `end`/`seconds` are null on "started": the stretch is still open, and the
    matching "finalized" carries the SAME `start` so the two pair up.

    Raises CeravisApiError on any failure; the caller (recording_events) sends
    once and never retries. A 2xx here is also what tells the cloud outbox the
    path is clear (on_reachable).
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/recordings/event"
    seg = payload.get("segment") or {}
    label = (f"{payload.get('camera_id')} {payload.get('status')} "
             f"{seg.get('seconds') if seg.get('seconds') is not None else ''}").strip()
    logger.info("recordingEvent -> POST %s  camera=%s  status=%s", url,
                payload.get("camera_id"), payload.get("status"))
    t0 = time.perf_counter()
    try:
        resp = _session().post(url, json=payload, headers=_headers(),
                             timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("recordingEvent: cannot reach %s — %s", url, exc)
        call_log.record("recordingEvent", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("recordingEvent", "POST", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("recordingEvent <- HTTP %s  body=%s",
                resp.status_code, _brief(resp.text, 200))
    _wire("recordingEvent", "POST", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        call_log.record("recordingEvent", False, label=label,
                        status=resp.status_code, latency_ms=lat,
                        error=_brief(resp.text, 200))
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    call_log.record("recordingEvent", True, label=label,
                    status=resp.status_code, latency_ms=lat)


def _multipart_headers() -> dict[str, str]:
    """Headers for a multipart/form-data upload: the API key + Accept, but NO
    Content-Type — `requests` sets `multipart/form-data` with the correct boundary
    when `files=` is passed, and forcing application/json here would break the
    server's multipart parsing."""
    h = {"Accept": "application/json"}
    key = settings.ceravis_api_key.strip()
    if key:
        h["X-API-Key"] = key
    return h


def save_snapshot(patient_id, text: str, camera_number: str, *,
                  image: bytes | None = None, video: bytes | None = None,
                  alert_id: int | None = None, category: str | None = None,
                  idempotency_key: str | None = None):
    """
    POST /v1/ai/saveSnapshot — the snapshot/clip for an alert or action.
    multipart/form-data (SnapshotRequest): patientId, text, cameraNumber, category
    as form fields, and the media as FILE parts:
      image : the JPEG still, raw bytes (MultipartFile `image`)
      video : the MP4 incident clip, raw bytes (MultipartFile `video`)
    Either or both may be sent — the still fires immediately, the fall clip
    follows once its post-roll footage is written; both carry the same text and,
    when present, alertId so the server associates them with the alert. `category`
    labels what the snapshot is about (the event/alert type — FALL, NO_MOTION, …).
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    if not image and not video:
        raise CeravisApiError("saveSnapshot: nothing to send (no image or video)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/saveSnapshot"
    # Multipart form fields must be strings — the encoder rejects a raw int, and
    # Spring converts "43" back to the Long/enum on its side.
    data = {"patientId": str(patient_id), "text": text or "",
            "cameraNumber": camera_number or ""}
    if alert_id is not None:
        data["alertId"] = str(alert_id)
    if category:
        data["category"] = category
    files = {}
    if image:
        files["image"] = ("snapshot.jpg", image, "image/jpeg")
    if video:
        files["video"] = ("incident.mp4", video, "video/mp4")
    # A loggable view of the request: the form fields verbatim + a byte-size
    # marker for each binary part (never the bytes), so the wire log stays small.
    log_body = dict(data)
    for part, blob in (("image", image), ("video", video)):
        if blob:
            log_body[part] = f"<{part}: {len(blob)} bytes>"
    parts = "+".join(k for k in ("image", "video") if k in files)
    logger.info("saveSnapshot -> POST %s  patient=%s  cam=%s  alert=%s  parts=%s",
                url, patient_id, camera_number, alert_id, parts)
    t0 = time.perf_counter()
    try:
        resp = _session().post(url, data=data, files=files,
                               headers=_with_key(_multipart_headers(), idempotency_key),
                               timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("saveSnapshot: cannot reach %s — %s", url, exc)
        call_log.record("saveSnapshot", False, label=text, alert_id=alert_id,
                        error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("saveSnapshot", "POST", url, log_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveSnapshot <- HTTP %s  body=%s", resp.status_code, _brief(resp.text, 200))
    _wire("saveSnapshot", "POST", url, log_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        call_log.record("saveSnapshot", False, label=text, alert_id=alert_id,
                        status=resp.status_code, latency_ms=lat,
                        error=_brief(resp.text, 200))
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code, retry_after=_retry_after(resp))
    try:
        result = _unwrap(resp.json())
    except ValueError:
        result = True
    call_log.record("saveSnapshot", True, label=text, alert_id=alert_id,
                    status=resp.status_code, latency_ms=lat,
                    link=call_log.find_link(result))
    return result


def get_patient_postures(user_id) -> list[dict]:
    """
    PUT /v1/ai/getPatientPostures — the patient's enrollment posture images
    (captured in the main CERAVIS app). Body: { "userId": <patientUserId> }.

    Returns the `data` list: one entry per postureType (STANDING, SITTING, …),
    each with up to four views — frontView / backView / leftView / rightView —
    and every view carries a signed `downloadUrl`. Views the patient never
    uploaded are null. Returns [] when the patient has none (or isn't found), so
    the enrollment step degrades to on-device capture instead of failing.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/getPatientPostures"
    request_body = {"userId": user_id}
    logger.info("getPatientPostures -> PUT %s  user=%s", url, user_id)
    t0 = time.perf_counter()
    try:
        resp = _session().put(url, json=request_body, headers=_headers(),
                            timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("getPatientPostures: cannot reach %s — %s", url, exc)
        call_log.record("getPatientPostures", False, label=str(user_id),
                        error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        # Persist the failed hit too, so "did it even fire?" is answerable.
        _wire("getPatientPostures", "PUT", url, request_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("getPatientPostures <- HTTP %s", resp.status_code)
    call_log.record("getPatientPostures", resp.status_code < 400,
                    label=str(user_id), status=resp.status_code, latency_ms=lat)
    _wire("getPatientPostures", "PUT", url, request_body,
          status=resp.status_code, response=resp.text, latency_ms=lat)
    if resp.status_code == 404:
        return []
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    try:
        data = _unwrap(resp.json())
    except ValueError as exc:
        raise CeravisApiError("app server returned non-JSON") from exc
    return data if isinstance(data, list) else []


def upload_embedding_file(file_category: str, user_id, file_name: str,
                          file_bytes: bytes):
    """
    PUT /v1/ai/uploadEmbeddingFile — store one generated artifact against the
    patient in the cloud so it survives a device reflash / follows the account.
    Body: { fileCategory, userId, fileName, fileBytes }.
      fileCategory : "EMBEDDING" (a recipient's .npy of ReID vectors) or
                     "ZONING" (the room zones.json)
      fileBytes    : the raw file, base64-encoded (no data-URI prefix)
    Returns the server payload; raises CeravisApiError on failure.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/uploadEmbeddingFile"
    payload = {
        "fileCategory": file_category,
        "userId": user_id,
        "fileName": file_name,
        "fileBytes": base64.b64encode(file_bytes).decode("ascii"),
    }
    label = f"{file_category} {file_name} ({len(file_bytes)} B)"
    logger.info("uploadEmbeddingFile -> PUT %s  user=%s  %s", url, user_id, label)
    t0 = time.perf_counter()
    try:
        resp = _session().put(url, json=payload, headers=_headers(),
                            timeout=_timeout())
    except requests.RequestException as exc:
        logger.warning("uploadEmbeddingFile: cannot reach %s — %s", url, exc)
        call_log.record("uploadEmbeddingFile", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("uploadEmbeddingFile", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("uploadEmbeddingFile <- HTTP %s  body=%s",
                resp.status_code, _brief(resp.text, 200))
    call_log.record("uploadEmbeddingFile", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    _wire("uploadEmbeddingFile", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True


def send_status(payload: dict) -> tuple[bool, int | None, str | None]:
    """POST the device status heartbeat. Returns (ok, http_status, error); never
    raises — a heartbeat must never be able to disturb the pipeline.

    Unlike the /v1/ai/* calls above it hits settings.status_heartbeat_url (NOT
    ceravis_api_base_url), but it carries the SAME X-API-Key header they do (via
    _headers()). Every beat is written to the forensic WIRE log (so `tail -f
    data/ceravis_api_wire.jsonl` shows it live), but NOT to call_log: a beat a
    minute would bury the Cloud Sync Console, so the caller records only the
    online/offline TRANSITIONS there."""
    url = settings.status_heartbeat_url.strip()
    t0 = time.perf_counter()
    try:
        resp = _session().post(url, json=payload, headers=_headers(),
                             timeout=_timeout())
    except requests.RequestException as exc:
        _wire("status", "POST", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        return False, None, str(exc)
    lat = (time.perf_counter() - t0) * 1000
    _wire("status", "POST", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code < 400:
        return True, resp.status_code, None
    return False, resp.status_code, f"HTTP {resp.status_code}: {_brief(resp.text, 120)}"
