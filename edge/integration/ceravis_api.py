from __future__ import annotations

"""
Thin client for the CERAVIS application server (Spring).

Only the calls the edge device needs. Everything goes through here so the
server address lives in ONE place (device config) and never in the browser.

EVERY call is SIGNED (see "Signed calls" below); none carries the API key:
    POST {base}/v1/ai/sendOtp          {"email", "deviceId"}
    POST {base}/v1/ai/verifyOtp        no body: the code is only inside the signature
    POST {base}/v1/ai/userDetails      {"email", "deviceId"}
    POST {base}/v1/ai/unassignDevice   {"email", "deviceId"}
    PUT  saveCamera, cameras/delete, deletePatientZoningFile, uploadEmbeddingFile,
         getPatientPostures, saveAlert; POST recordings/event — JSON, deviceId first
    POST saveSnapshot                  multipart, with a deviceId field
    deviceId is THIS device's name (common/device_identity) — the same device_id
    the fleet agent enrolls with, so the server finds the device's FMS record
    (edge_id, …) in the shared DB and maps the account's cameras and live links.
    userDetails -> ApiResponse<UserDetailsResponse>
       { ceravisUserId, firstName, lastName, email, gender, role, tier,
         deviceToken }
    deviceToken is the app server's own token for this device. The edge routes
    on the edge_id the Fleet Management Server issued instead; the token is kept
    only to notice a mismatch (the status beat tells the server the real one).
A user that exists in the server's DB is what "verifies" the operator; if the
email isn't found, onboarding is blocked.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path

import requests

from config.settings import resolve_path, settings
from integration import call_log
from common import clock
from common.atomic import write_atomic
from common.device_identity import identity


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
_WIRE_MAX_BYTES = 16 * 1024 * 1024      # rotate past this… (room for 1500 typical records)
_WIRE_KEEP_LINES = 1500                  # …down to the newest N records
# Request fields that are large base64 blobs — logged as a short marker, never
# verbatim, so the wire log stays readable and small.
# Big base64 fields to collapse to a size marker in the wire log. saveSnapshot no
# longer sends base64 (it uploads image/video as multipart files, logged as byte-
# size markers by the caller); uploadEmbeddingFile still base64-encodes fileBytes.
_WIRE_REDACT_KEYS = ("fileBytes",)
# Reply fields that are key material (sendOtp's randomId makes this device's
# key): withheld from the wire log wherever they appear.
_WIRE_SECRET_KEYS = frozenset({"randomId"})


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


def _wire_scrub(text: str | None) -> str | None:
    """A reply safe to persist: every key-material field withheld, the rest as
    received. A reply that names one but cannot be read is withheld whole."""
    if not text or not any(k in text for k in _WIRE_SECRET_KEYS):
        return text

    def scrub(v):
        if isinstance(v, dict):
            return {k: "<withheld>" if k in _WIRE_SECRET_KEYS else scrub(x) for k, x in v.items()}
        return [scrub(x) for x in v] if isinstance(v, list) else v
    try:
        return json.dumps(scrub(json.loads(text)), ensure_ascii=False)
    except (ValueError, RecursionError):
        return "<withheld: the reply holds key material>"


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
            "response": _wire_scrub(response),
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
    if not key:
        return ("app-server API key not installed — the app server refuses this "
                "device's sign-in, alerts and snapshots (uploads queued, not lost); "
                "run setup/install_api_key.sh")
    if hashlib.sha256(key.encode("utf-8")).hexdigest() in _REVOKED_API_KEYS_SHA256:
        return ("app-server API key is the old revoked one (it was public) — the app "
                "server refuses this device's sign-in; install the new key: "
                "setup/install_api_key.sh --replace")
    if not _edge_key():
        from config.account_config import AccountConfig
        if AccountConfig().get().get("ceravisUserId"):
            return ("this device has no app-server key yet — sign in once with a code; "
                    "until then the app server refuses its alerts and uploads (kept, not lost)")
    return None


# Keys the app server no longer accepts, by their SHA-256 only (the old one was
# public: it used to be this repository's built-in default, retired 2026-11-19).
_REVOKED_API_KEYS_SHA256 = frozenset({
    "b4dee005f91e1641f1b557b20f2945e2d593d5dcb4a24cc05a30c15f8b2989c7",
})


def _headers() -> dict[str, str]:
    # Only the status beat (send_status — another address, off unless
    # STATUS_HEARTBEAT_URL is set) still carries the API key, in "X-API-Key".
    # Every /v1/ai call is signed instead (see "Signed calls").
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    key = settings.ceravis_api_key.strip()
    if key:
        h["X-API-Key"] = key
    return h


# ---------------------------------------------------------------------------
# Signed calls — EVERY call to the app server.
#
# No call carries the API key. Each request is signed, and the server signs
# its reply the same way, so neither side acts on a message the other did not
# send, and no secret ever crosses the network:
#
#   request  X-Ceravis-Timestamp  Unix seconds, three decimals: 1793361045.218
#            X-Ceravis-Signature  Base64( HMAC-SHA256(key, timestamp "\n" sha256hex(payload)) )
#   reply    X-Ceravis-Signature  Base64( HMAC-SHA256(key, request timestamp "\n" sha256hex(reply body)) )
#
# `payload` is the request body exactly as sent (compact UTF-8 JSON). verifyOtp
# sends no body: its payload is the code the person typed. saveSnapshot is
# multipart: its payload is a manifest of its fields and files
# (_snapshot_manifest). sha256hex is lowercase hex. A 2xx reply is acted on
# only when its signature matches. Every body names the device by deviceId;
# verifyOtp names nothing, so the server finds its code by the signature alone.
#
# The key — one rule. A sendOtp reply's randomId makes the device's next key:
#     K' = hex( HMAC-SHA256( current key, randomId ) )      64 lowercase hex
# the current key being the device's own key, or the API key before its first
# sign-in. K' is held in memory and signs that sign-in's verifyOtp; once the
# server confirms the code it replaces the device key (<data>/edge_api.key,
# mode 0600) and signs every call after. Until then the old key stays. So the
# API key signs only a device's very first sendOtp, and the key is renewed at
# every sign-in. It belongs to the device: removing the care recipient keeps
# it, setup/seal.sh removes it. A randomId, a code and a key never reach a log.
#
# The clock. The server accepts a timestamp within a few seconds of its own
# clock. A device clock that is off (no NTP yet, a dead clock battery) is
# measured from every reply's Date header and corrected, and a call refused
# for it is signed again, once (_learn_clock).
# ---------------------------------------------------------------------------
HDR_TIMESTAMP = "X-Ceravis-Timestamp"
HDR_SIGNATURE = "X-Ceravis-Signature"
EDGE_KEY_FILE = "edge_api.key"
# How long a code request's key waits for its code (the server's code expires sooner).
_PENDING_TTL_SECS = 900.0
_UNVERIFIED = "The CERAVIS server's reply could not be verified. Please try again."
# A clock gap worth correcting; a smaller one is the Date header's own
# one-second resolution, well inside the server's window.
_CLOCK_GAP_SECS = 2.0
_clock_offset = 0.0                    # server time - this device's time, in seconds


def _signature(key: str, timestamp: str, payload: bytes) -> str:
    message = f"{timestamp}\n{hashlib.sha256(payload).hexdigest()}".encode("utf-8")
    mac = hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def _signed_by(key: str, timestamp: str, payload: bytes, signature: str | None) -> bool:
    return hmac.compare_digest(_signature(key, timestamp, payload).encode("ascii"),
                               (signature or "").strip().encode("utf-8", "replace"))


def _derive_edge_key(key: str, random_id: str) -> str:
    """The device's next key: HMAC-SHA256(key = its current key — the API key
    before its first sign-in — message = a sendOtp reply's randomId), as 64
    lowercase hex characters."""
    return hmac.new(key.strip().encode("utf-8"), random_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _now() -> float:
    """Unix time as the app server counts it (see _learn_clock)."""
    return time.time() + _clock_offset


def _learn_clock(resp, sent_at: float) -> bool:
    """Measure this device's clock against the reply's Date header (a whole
    second, so +0.5 s) and keep the gap when it exceeds _CLOCK_GAP_SECS. True
    when the correction changed by that much — a refused call is worth signing
    again."""
    global _clock_offset
    try:
        server = parsedate_to_datetime(resp.headers.get("Date")).timestamp() + 0.5
    except (TypeError, ValueError, IndexError, OverflowError):
        return False
    gap = server - (sent_at + time.time()) / 2
    offset = gap if abs(gap) > _CLOCK_GAP_SECS else 0.0
    changed = abs(offset - _clock_offset) > _CLOCK_GAP_SECS
    if changed and offset:
        logger.warning("this device's clock is %.1f s %s the app server's — calls are signed "
                       "with the server's time (is NTP running?)", abs(offset),
                       "behind" if offset > 0 else "ahead of")
    _clock_offset = offset
    return changed


def _edge_key_path() -> Path:
    return resolve_path(settings.data_dir) / EDGE_KEY_FILE


def _edge_key() -> str | None:
    """The key this device signs its sign-in calls with; None until its first
    sign-in is confirmed."""
    try:
        return _edge_key_path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# The next key a code request made, held (in memory only) until its code checks out.
_pending: dict = {}
_pending_lock = threading.Lock()


def _hold_pending(email: str, key: str) -> None:
    with _pending_lock:
        _pending.clear()
        _pending.update(email=email.casefold(), key=key,
                        expires=time.monotonic() + _PENDING_TTL_SECS)


def _drop_pending() -> None:
    with _pending_lock:
        _pending.clear()


def _pending_key(email: str) -> str | None:
    with _pending_lock:
        if _pending.get("email") == email.casefold() and _pending["expires"] > time.monotonic():
            return _pending["key"]
    return None


def _keep_edge_key(key: str) -> None:
    write_atomic(_edge_key_path(), key, mode=0o600)
    _drop_pending()
    logger.info("this device's app-server key is renewed; every call is signed with it from now on")


def _random_id(body) -> str | None:
    """sendOtp's randomId, verbatim, or None when the reply has no usable one."""
    data = _unwrap(body)
    value = data.get("randomId") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip() and len(value) <= 256 else None


def _json_bytes(body: dict) -> bytes:
    """A JSON body's exact bytes — what is sent is what is signed."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _signed_call(method: str, path: str, key: str, *, name: str, body: bytes = b"",
                 payload: bytes | None = None, form: dict | None = None,
                 files: dict | None = None, headers: dict | None = None,
                 kind: str | None = None, label: str = "", log_request=None):
    """Send one signed call to {base}/v1/ai/<path>: `body` as sent (JSON bytes —
    or the multipart `form` and `files`), signed over `payload` (the body itself
    unless given). A call refused because this device's clock is off is signed
    again, once, with the server's time. Returns (reply, latency_ms, trusted) —
    `trusted` = the reply carries the server's signature over its exact bytes.
    Raises CeravisApiError when the server cannot be reached."""
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/" + path
    signed = body if payload is None else payload
    send = getattr(_session(), method.lower())
    for attempt in (1, 2):
        timestamp = f"{_now():.3f}"
        h = {"Accept": "application/json", **(headers or {}),
             HDR_TIMESTAMP: timestamp, HDR_SIGNATURE: _signature(key, timestamp, signed)}
        if body:
            h["Content-Type"] = "application/json"
        sent_at, t0 = time.time(), time.perf_counter()
        try:
            resp = send(url, data=form if files else body, files=files, headers=h,
                        timeout=_timeout())
        except requests.RequestException as exc:
            lat = (time.perf_counter() - t0) * 1000
            logger.warning("%s: cannot reach %s — %s", name, url, exc)
            call_log.record(kind or name, False, label=label, error=str(exc), latency_ms=lat)
            _wire(name, method, url, log_request, error=str(exc), latency_ms=lat)
            raise CeravisApiError(f"cannot reach app server: {exc}") from exc
        lat = (time.perf_counter() - t0) * 1000
        if not (_learn_clock(resp, sent_at) and resp.status_code == 401 and attempt == 1):
            break
    trusted = _signed_by(key, timestamp, resp.content, resp.headers.get(HDR_SIGNATURE))
    logger.info("%s <- HTTP %s%s", name, resp.status_code,
                "" if trusted or resp.status_code >= 300 else " — reply signature does not match, not acted on")
    _wire(name, method, url, log_request, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    return resp, lat, trusted


def _signing_key() -> str:
    """The device key every call after sign-in is signed with. Raises when the
    device has none yet: before its first sign-in nothing can be sent (an
    upload stays queued)."""
    if not is_configured():
        raise CeravisApiError("CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    key = _edge_key()
    if not key:
        raise CeravisApiError("this device has no app-server key yet — sign in once with a code",
                              status=401)
    return key


# saveSnapshot is multipart: Spring parses the parts, so its signature covers a
# manifest of them rather than the raw bytes — one line per field in this fixed
# order, "name=" + sha256hex of the value's UTF-8 bytes (of the file's bytes
# for image and video), nothing after "=" when absent or empty; "\n" between
# the lines, none at the end.
_MANIFEST = ("deviceId", "patientId", "text", "cameraNumber", "alertId", "category",
             "image", "video")


def _snapshot_manifest(form: dict, files: dict) -> bytes:
    def digest(name: str) -> str:
        value = files[name][1] if name in files else form.get(name)
        if isinstance(value, str):
            value = value.encode("utf-8")
        return hashlib.sha256(value).hexdigest() if value else ""
    return "\n".join(f"{n}={digest(n)}" for n in _MANIFEST).encode("utf-8")


def _idempotency(key: str | None) -> dict | None:
    """The outbox job id, identical on every retry of one upload, so the server
    keeps one copy however many tries reach it."""
    return {"Idempotency-Key": key} if key else None


def _send_data(method: str, path: str, *, name: str, body: dict | None = None,
               form: dict | None = None, files: dict | None = None,
               headers: dict | None = None, kind: str | None = None, label: str = "",
               absent=None, logged: dict | None = None, report=None):
    """One signed data call — every call but the sign-in ones. The JSON body (or
    the multipart form) names this device first (deviceId); the request is
    signed with the device key; the reply is used only when the server's
    signature over it matches. Returns the reply's `data` (True when it has
    none), or `absent` for a 404 when given. Raises CeravisApiError on any
    failure. `logged` / `report(result)` add fields to the call-log line."""
    key = _signing_key()
    kind = kind or name
    device = {"deviceId": identity().name}
    if form is None:
        body = {**device, **(body or {})}
        raw, payload, log_request = _json_bytes(body), None, body
    else:
        form = {**device, **form}
        raw, payload = b"", _snapshot_manifest(form, files or {})
        log_request = {**form, **{k: f"<{k}: {len(f[1])} bytes>" for k, f in (files or {}).items()}}
    resp, lat, trusted = _signed_call(method, path, key, name=name, body=raw, payload=payload,
                                      form=form, files=files, headers=headers, kind=kind,
                                      label=label, log_request=log_request)
    status = resp.status_code
    if status == 404 and absent is not None:
        call_log.record(kind, True, label=label, status=status, latency_ms=lat, **(logged or {}))
        return absent
    if status >= 400 or not trusted:
        error = _brief(resp.text, 200) if status >= 400 else "reply signature does not match"
        call_log.record(kind, False, label=label, status=status, latency_ms=lat, error=error,
                        **(logged or {}))
        if status >= 400:
            raise CeravisApiError(f"app server returned HTTP {status}: {error}",
                                  status=status, retry_after=_retry_after(resp))
        raise CeravisApiError(_UNVERIFIED, status=status)
    try:
        result = _unwrap(resp.json())
    except ValueError:
        result = True
    call_log.record(kind, True, label=label, status=status, latency_ms=lat,
                    **(logged or {}), **(report(result) if report else {}))
    return result


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
    POST /v1/ai/userDetails {"email", "deviceId"}, signed with this device's
    key (it has one by now: verify_otp keeps it once the code checks out).
    Returns the UserDetailsResponse dict if the account exists, None if no such
    user. Raises CeravisApiError on config/transport/server errors and on a
    reply whose signature does not match (so the caller can show a real reason).
    """
    key = _signing_key()
    # identity().name, never settings.device_id (DEVICE_ID may override that):
    # it must equal the device_id the FMS stored at enrollment.
    device_id = identity().name
    request_body = {"email": email, "deviceId": device_id}
    logger.info("userDetails -> email=%s  deviceId=%s", email, device_id)
    resp, lat, trusted = _signed_call("POST", "userDetails", key, name="userDetails",
                                      body=_json_bytes(request_body), label=email,
                                      log_request=request_body)
    call_log.record("userDetails", resp.status_code < 400 and trusted, label=email,
                    status=resp.status_code, latency_ms=lat)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning("userDetails error body: %s", _brief(resp.text, 300))
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}",
            status=resp.status_code)
    if not trusted:
        raise CeravisApiError(_UNVERIFIED, status=resp.status_code)
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
    POST /v1/ai/sendOtp — ask the app server to email a one-time code (to sign
    in, or to confirm removing the care recipient). Body: {"email": "...",
    "deviceId": "ceravis-<code>"}, signed with this device's key — with the API
    key before its first sign-in. The reply's randomId makes the device's next
    key from that same key, held until the code checks out (verify_otp).

    Returns (True, None) when the server takes it, (False, reason) when it
    refuses — a 4xx, or a 2xx whose body says it failed or whose signature does
    not match — with the server's own message as the reason when it gives one.
    Raises CeravisApiError on config/transport/5xx errors. The exchange is
    written to the wire log (data/ceravis_api_wire.jsonl), the randomId withheld.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    device_key = _edge_key()
    api_key = settings.ceravis_api_key.strip()
    if not (device_key or api_key):
        return False, "This device has no app-server API key installed (setup/install_api_key.sh)."
    request_body = {"email": email, "deviceId": identity().name}
    logger.info("sendOtp -> email=%s  signed with %s", email,
                "this device's key" if device_key else "the API key (first sign-in)")
    resp, lat, trusted = _signed_call("POST", "sendOtp", device_key or api_key, name="sendOtp",
                                      body=_json_bytes(request_body), label=email,
                                      log_request=request_body)
    if resp.status_code >= 500:
        call_log.record("sendOtp", False, label=email, status=resp.status_code,
                        latency_ms=lat)
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    message = _server_message(resp)
    body = _json_or_none(resp)
    reason = None
    if resp.status_code == 404:
        reason = message or "No CERAVIS account found for this email"
    elif not 200 <= resp.status_code < 300:
        reason = message or f"The CERAVIS server refused the request (HTTP {resp.status_code})"
    elif not trusted:
        reason = _UNVERIFIED
        _drop_pending()                  # the server may have replaced its code: a key held from before is stale
    elif _send_refused(body):
        reason = ((message if message and not _GENERIC_OK.match(message) else None)
                  or "The CERAVIS server did not send the code")
    elif random_id := _random_id(body):
        _hold_pending(email, _derive_edge_key(device_key or api_key, random_id))
    elif device_key is None:
        reason = "The CERAVIS server sent no key for this device. Please try again."
    else:
        _drop_pending()                  # this code is checked with the device's key
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
    POST /v1/ai/verifyOtp — check the one-time code. It sends NO body: the code
    is only the signature's payload, so it never crosses the network and never
    reaches a log. Signed with the next key its sendOtp reply made, which then
    replaces the device key once the server confirms the code (with the
    device's key when that reply carried no randomId).

    Fails CLOSED: returns (True, None, account) only for the server's one
    confirmation (_otp_confirmed: HTTP 2xx with a matching signature, code 1,
    "OTP verified successfully"), `account` being the user it confirmed — its
    `data`: ceravisUserId, deviceToken (the app server's token for it), names,
    role, tier — or None when the reply carries none. Any other reply gives
    (False, reason, None), the reason being the server's message exactly as
    sent ("Invalid OTP", "No OTP found…", …). Raises CeravisApiError on config/
    transport/5xx errors. The reply is captured in the wire log — that record
    shows the exact reply shape if a correct code is ever refused.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    next_key = _pending_key(email)
    key = next_key or _edge_key()
    if not key:
        return False, "Ask for a new code first, then enter it here.", None
    logger.info("verifyOtp -> email=%s", email)
    resp, lat, trusted = _signed_call("POST", "verifyOtp", key, name="verifyOtp",
                                      payload=otp.encode("utf-8"), label=email)
    if resp.status_code >= 500:
        call_log.record("verifyOtp", False, label=email, status=resp.status_code,
                        latency_ms=lat)
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    answered = 200 <= resp.status_code < 300
    body = _json_or_none(resp) if answered and trusted else None
    ok = _otp_confirmed(body)
    reason = None
    if ok and next_key:
        try:
            _keep_edge_key(next_key)
        except OSError as exc:
            logger.error("this device's app-server key could not be saved: %s", exc)
            ok, reason = False, "This device could not save its sign-in key. Please try again."
    account = body.get("data") if ok and isinstance(body.get("data"), dict) else None
    if reason is None and not ok:
        message = _server_message(resp)
        reason = (_UNVERIFIED if answered and not trusted
                  else (message if message and not _GENERIC_OK.match(message) else None)
                  or _OTP_REFUSED.get(resp.status_code)
                  or ("The CERAVIS server did not confirm the code. Please try again."
                      if answered else "Invalid or expired code"))
    call_log.record("verifyOtp", ok, label=email, error=reason,
                    status=resp.status_code, latency_ms=lat)
    return ok, reason, account


def unassign_device(email: str) -> tuple[bool, str | None]:
    """
    POST /v1/ai/unassignDevice {"email", "deviceId"} — the app server lets this
    device go from its care recipient (removing them, once their code checked
    out). Signed with the device key. Returns (True, None) only for the
    server's confirmation — a 2xx whose signature matches, code 1 — and
    (False, reason) otherwise. Raises CeravisApiError when the server cannot be
    reached or fails (5xx).
    """
    key = _signing_key()
    request_body = {"email": email, "deviceId": identity().name}
    logger.info("unassignDevice -> email=%s  deviceId=%s", email, request_body["deviceId"])
    resp, lat, trusted = _signed_call("POST", "unassignDevice", key, name="unassignDevice",
                                      body=_json_bytes(request_body), label=email,
                                      log_request=request_body)
    if resp.status_code >= 500:
        call_log.record("unassignDevice", False, label=email, status=resp.status_code,
                        latency_ms=lat)
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {_brief(resp.text, 200)}",
            status=resp.status_code)
    answered = 200 <= resp.status_code < 300
    body = _json_or_none(resp) if answered and trusted else None
    ok = isinstance(body, dict) and _flag(body.get("code")) is True
    reason = None
    if not ok:
        message = _server_message(resp)
        reason = (_UNVERIFIED if answered and not trusted
                  else (message if message and not _GENERIC_OK.match(message) else None)
                  or "The CERAVIS server did not unassign this device.")
    call_log.record("unassignDevice", ok, label=email, error=reason,
                    status=resp.status_code, latency_ms=lat)
    return ok, reason


def save_cameras(patient_user_id, cameras: list[dict]):
    """
    PUT /v1/ai/saveCamera — register this patient's cameras with the app server.

    Body:
        { "deviceId": "ceravis-<code>", "patientUserId": <id>,
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
    rooms = [c.get("room") for c in cameras]
    logger.info("saveCamera -> patient=%s  cameras=%d  rooms=%s", patient_user_id,
                len(cameras), rooms)
    return _send_data("PUT", "saveCamera", name="saveCamera",
                      label=f"{len(cameras)} camera(s): " + ", ".join(r or "?" for r in rooms),
                      body={"patientUserId": patient_user_id, "cameras": cameras})


def delete_camera(patient_user_id, room: str):
    """
    PUT /v1/ai/cameras/delete — remove ONE camera from this patient's account on
    the app server, mirroring the device's local delete so the cloud never keeps
    a camera the edge no longer has. Body: { "deviceId", "userId": <id>, "room":
    <CameraName enum, e.g. LIVING_ROOM> }. Returns the server response; raises
    CeravisApiError on failure (the caller treats it as best-effort so a cloud
    hiccup never blocks the local delete).
    """
    logger.info("cameras/delete -> user=%s  room=%s", patient_user_id, room)
    return _send_data("PUT", "cameras/delete", name="cameraDelete",
                      label=f"delete camera: {room}",
                      body={"userId": patient_user_id, "room": room})


def delete_patient_zoning_file(user_id):
    """
    PUT /v1/ai/deletePatientZoningFile — remove this patient's existing ZONING
    file from cloud storage (S3) so a re-save writes a clean object instead of
    leaving a stale one behind (S3 objects can't be edited in place). Body:
    { "deviceId", "userId": <id> }. Called right before
    uploadEmbeddingFile("ZONING", …) so the cloud ends up with exactly one,
    current zones file per patient.

    A 404 means there was no prior file — that's a success for delete-then-write,
    so it returns True rather than raising. Returns the server response otherwise;
    raises CeravisApiError on a real failure. Callers treat it as best-effort:
    the delete is cleanup, and must never block saving the new zones file.
    """
    logger.info("deletePatientZoningFile -> user=%s", user_id)
    return _send_data("PUT", "deletePatientZoningFile", name="deletePatientZoningFile",
                      kind="deleteZoningFile", label=f"delete zoning file: user {user_id}",
                      body={"userId": user_id}, absent=True)


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
    Body: { deviceId, patientUserId, alertType (e.g. "FALL"), messageText }.
    Returns the server's response payload (carries the new alert's id — see
    alert_id_of); raises CeravisApiError on failure.
    `idempotency_key` (the outbox job id) goes out as the Idempotency-Key
    header, identical on every retry of the same alert (each retry is signed
    anew), so the server drops the duplicate a timed-out-but-committed try leaves.
    """
    logger.info("saveAlert -> patient=%s  type=%s", patient_user_id, alert_type)
    return _send_data("PUT", "saveAlert", name="saveAlert",
                      label=f"{alert_type} · {message_text}",
                      body={"patientUserId": patient_user_id, "alertType": alert_type,
                            "messageText": message_text},
                      headers=_idempotency(idempotency_key),
                      report=lambda result: {"alert_id": alert_id_of(result)})


def send_recording_event(payload: dict) -> None:
    """
    POST /v1/ai/recordings/event — tell the app server that ONE camera has
    started, or finished, recording a stretch of footage.

    Body is the backend's TimelineSegmentEvent, key names verbatim (note the
    deliberate camelCase/snake_case mix — that is what the Java DTO declares),
    after this device's deviceId:

        { "deviceId": "ceravis-<code>", "edgeId": "<edge_id>",
          "camera_id": "LIVING_ROOM", "status": "started" | "finalized",
          "segment": { "start": ISO-8601, "end": ISO-8601|null,
                       "seconds": float|null } }

    `end`/`seconds` are null on "started": the stretch is still open, and the
    matching "finalized" carries the SAME `start` so the two pair up.

    Raises CeravisApiError on any failure; the caller (recording_events) sends
    once and never retries. A 2xx here is also what tells the cloud outbox the
    path is clear (on_reachable).
    """
    seg = payload.get("segment") or {}
    logger.info("recordingEvent -> camera=%s  status=%s", payload.get("camera_id"),
                payload.get("status"))
    _send_data("POST", "recordings/event", name="recordingEvent", body=payload,
               label=(f"{payload.get('camera_id')} {payload.get('status')} "
                      f"{seg.get('seconds') if seg.get('seconds') is not None else ''}").strip())


def save_snapshot(patient_id, text: str, camera_number: str, *,
                  image: bytes | None = None, video: bytes | None = None,
                  alert_id: int | None = None, category: str | None = None,
                  idempotency_key: str | None = None):
    """
    POST /v1/ai/saveSnapshot — the snapshot/clip for an alert or action.
    multipart/form-data (SnapshotRequest): deviceId, patientId, text,
    cameraNumber, alertId, category as form fields, and the media as FILE parts:
      image : the JPEG still, raw bytes (MultipartFile `image`)
      video : the MP4 incident clip, raw bytes (MultipartFile `video`)
    Either or both may be sent — the still fires immediately, the fall clip
    follows once its post-roll footage is written; both carry the same text and,
    when present, alertId so the server associates them with the alert. `category`
    labels what the snapshot is about (the event/alert type — FALL, NO_MOTION, …).
    Signed over the manifest of these parts (_snapshot_manifest).
    """
    if not image and not video:
        raise CeravisApiError("saveSnapshot: nothing to send (no image or video)")
    # Multipart form fields must be strings — the encoder rejects a raw int, and
    # Spring converts "43" back to the Long/enum on its side.
    form = {"patientId": str(patient_id), "text": text or "",
            "cameraNumber": camera_number or ""}
    if alert_id is not None:
        form["alertId"] = str(alert_id)
    if category:
        form["category"] = category
    files = {}
    if image:
        files["image"] = ("snapshot.jpg", image, "image/jpeg")
    if video:
        files["video"] = ("incident.mp4", video, "video/mp4")
    logger.info("saveSnapshot -> patient=%s  cam=%s  alert=%s  parts=%s", patient_id,
                camera_number, alert_id, "+".join(files))
    return _send_data("POST", "saveSnapshot", name="saveSnapshot", label=text,
                      form=form, files=files, headers=_idempotency(idempotency_key),
                      logged={"alert_id": alert_id},
                      report=lambda result: {"link": call_log.find_link(result)})


def get_patient_postures(user_id) -> list[dict]:
    """
    PUT /v1/ai/getPatientPostures — the patient's enrollment posture images
    (captured in the main CERAVIS app). Body: { "deviceId", "userId":
    <patientUserId> }.

    Returns the `data` list: one entry per postureType (STANDING, SITTING, …),
    each with up to four views — frontView / backView / leftView / rightView —
    and every view carries a signed `downloadUrl`. Views the patient never
    uploaded are null. Returns [] when the patient has none (or isn't found), so
    the enrollment step degrades to on-device capture instead of failing.
    """
    logger.info("getPatientPostures -> user=%s", user_id)
    data = _send_data("PUT", "getPatientPostures", name="getPatientPostures",
                      label=str(user_id), body={"userId": user_id}, absent=[])
    return data if isinstance(data, list) else []


def upload_embedding_file(file_category: str, user_id, file_name: str,
                          file_bytes: bytes):
    """
    PUT /v1/ai/uploadEmbeddingFile — store one generated artifact against the
    patient in the cloud so it survives a device reflash / follows the account.
    Body: { deviceId, fileCategory, userId, fileName, fileBytes }.
      fileCategory : "EMBEDDING" (a recipient's .npy of ReID vectors) or
                     "ZONING" (the room zones.json)
      fileBytes    : the raw file, base64-encoded (no data-URI prefix)
    Returns the server payload; raises CeravisApiError on failure.
    """
    label = f"{file_category} {file_name} ({len(file_bytes)} B)"
    logger.info("uploadEmbeddingFile -> user=%s  %s", user_id, label)
    return _send_data("PUT", "uploadEmbeddingFile", name="uploadEmbeddingFile", label=label,
                      body={"fileCategory": file_category, "userId": user_id,
                            "fileName": file_name,
                            "fileBytes": base64.b64encode(file_bytes).decode("ascii")})


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
