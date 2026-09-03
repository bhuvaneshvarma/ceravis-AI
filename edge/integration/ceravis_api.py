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
from pathlib import Path

import requests

from config.settings import settings
from integration import call_log
from common import clock


logger = logging.getLogger("integration")

_EDGE_ROOT = Path(__file__).resolve().parents[1]

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


def _wire(endpoint: str, method: str, url: str, request, *,
          status: int | None = None, response: str | None = None,
          error: str | None = None, latency_ms: float | None = None) -> None:
    """Append one full request/response record to data/ceravis_api_wire.jsonl.
    Best-effort — any failure here is swallowed and never affects the call."""
    try:
        base = settings.data_path
        base = base if base.is_absolute() else (_EDGE_ROOT / base)
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

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_configured() -> bool:
    return bool(settings.ceravis_api_base_url.strip())


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
        resp = requests.post(url, json=request_body, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
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
        logger.warning("userDetails error body: %s", resp.text[:300])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}",
            status=resp.status_code)
    try:
        data = _unwrap(resp.json())
    except ValueError as exc:
        raise CeravisApiError("app server returned non-JSON") from exc
    if not isinstance(data, dict) or not data.get("ceravisUserId"):
        return None                              # account not found / empty
    return data


def send_otp(email: str) -> bool:
    """
    POST /v1/ai/sendOtp — ask the app server to email a login one-time code.
    Body: {"email": "..."}. Carries the X-API-Key header like every /v1/ai call.

    Returns True when the server accepts it (2xx). 404 -> False (no such
    account). Raises CeravisApiError on config/transport/other-server errors.
    The full request+response is written to the wire log (data/
    ceravis_api_wire.jsonl) so the exchange is inspectable by hand.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/sendOtp"
    request_body = {"email": email}
    logger.info("sendOtp -> POST %s  email=%s", url, email)
    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json=request_body, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("sendOtp: cannot reach %s — %s", url, exc)
        call_log.record("sendOtp", False, label=email, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("sendOtp", "POST", url, request_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("sendOtp <- HTTP %s", resp.status_code)
    call_log.record("sendOtp", resp.status_code < 400, label=email,
                    status=resp.status_code, latency_ms=lat)
    _wire("sendOtp", "POST", url, request_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code == 404:
        return False
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code)
    return True


def verify_otp(email: str, otp: str) -> bool:
    """
    POST /v1/ai/verifyOtp — check the login one-time code.
    Body: {"email": "...", "otp": "..."}. X-API-Key header as usual.

    Returns True when the server accepts the code (2xx, and not an explicit
    {data:false}); a bad/expired code (400/401/403/422, or {data:false}) -> False.
    Raises CeravisApiError on config/transport/other-server errors. Full
    request+response is captured in the wire log.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/verifyOtp"
    request_body = {"email": email, "otp": otp}
    logger.info("verifyOtp -> POST %s  email=%s", url, email)
    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json=request_body, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("verifyOtp: cannot reach %s — %s", url, exc)
        call_log.record("verifyOtp", False, label=email, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("verifyOtp", "POST", url, request_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("verifyOtp <- HTTP %s", resp.status_code)
    call_log.record("verifyOtp", resp.status_code < 400, label=email,
                    status=resp.status_code, latency_ms=lat)
    _wire("verifyOtp", "POST", url, request_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code in (400, 401, 403, 422):
        return False                              # wrong / expired code
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code)
    try:                                          # honour an explicit {data:false}
        if _unwrap(resp.json()) is False:
            return False
    except ValueError:
        pass
    return True


def save_cameras(patient_user_id, cameras: list[dict]):
    """
    PUT /v1/ai/saveCamera — register this patient's cameras with the app server.

    Body:
        { "patientUserId": <id>,
          "cameras": [ { device, model, supplier, room, url, rtspUrl,
                         recordRtspUrl, hlsUrl, onvifXaddr, onvifUsername,
                         onvifPassword, onvifProfileToken, onvifPtzToken,
                         ptzSupported, isEnabled, webrtcUrl }, ... ] }
    Each camera dict is the full cameras.json record in the server's camelCase
    shape (built by account_routes._cloud_camera) — the SAME details we persist
    on the edge, so the cloud mirrors the device exactly. Returns the server's
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
        resp = requests.put(url, json=payload, headers=_headers(),
                            timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("saveCamera: cannot reach %s — %s", url, exc)
        call_log.record("saveCamera", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("saveCamera", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveCamera <- HTTP %s  body=%s", resp.status_code, resp.text[:300])
    call_log.record("saveCamera", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    _wire("saveCamera", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
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
        resp = requests.put(url, json=payload, headers=_headers(),
                            timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("cameras/delete: cannot reach %s — %s", url, exc)
        call_log.record("cameraDelete", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("cameraDelete", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("cameras/delete <- HTTP %s  body=%s", resp.status_code, resp.text[:200])
    call_log.record("cameraDelete", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    _wire("cameraDelete", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
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


def save_alert(patient_user_id, alert_type: str, message_text: str):
    """
    PUT /v1/ai/saveAlert — push one alert to the app server.
    Body: { patientUserId, alertType (e.g. "FALL"), messageText }.
    Returns the server's response payload (carries the new alert's id — see
    alert_id_of); raises CeravisApiError on failure.
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
        resp = requests.put(url, json=payload, headers=_headers(),
                            timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("saveAlert: cannot reach %s — %s", url, exc)
        call_log.record("saveAlert", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("saveAlert", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveAlert <- HTTP %s  body=%s", resp.status_code, resp.text[:300])
    _wire("saveAlert", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        call_log.record("saveAlert", False, label=label,
                        status=resp.status_code, latency_ms=lat,
                        error=resp.text[:200])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code)
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

    Raises CeravisApiError on any failure — the caller is the outbox, so a
    failed event is retried rather than lost.
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
        resp = requests.post(url, json=payload, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("recordingEvent: cannot reach %s — %s", url, exc)
        call_log.record("recordingEvent", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("recordingEvent", "POST", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("recordingEvent <- HTTP %s  body=%s",
                resp.status_code, resp.text[:200])
    _wire("recordingEvent", "POST", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        call_log.record("recordingEvent", False, label=label,
                        status=resp.status_code, latency_ms=lat,
                        error=resp.text[:200])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
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
                  alert_id: int | None = None, category: str | None = None):
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
        resp = requests.post(url, data=data, files=files,
                             headers=_multipart_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("saveSnapshot: cannot reach %s — %s", url, exc)
        call_log.record("saveSnapshot", False, label=text, alert_id=alert_id,
                        error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("saveSnapshot", "POST", url, log_body, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveSnapshot <- HTTP %s  body=%s", resp.status_code, resp.text[:200])
    _wire("saveSnapshot", "POST", url, log_body, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        call_log.record("saveSnapshot", False, label=text, alert_id=alert_id,
                        status=resp.status_code, latency_ms=lat,
                        error=resp.text[:200])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code)
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
        resp = requests.put(url, json=request_body, headers=_headers(),
                            timeout=settings.ceravis_api_timeout_secs)
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
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
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
        resp = requests.put(url, json=payload, headers=_headers(),
                            timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("uploadEmbeddingFile: cannot reach %s — %s", url, exc)
        call_log.record("uploadEmbeddingFile", False, label=label, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        _wire("uploadEmbeddingFile", "PUT", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("uploadEmbeddingFile <- HTTP %s  body=%s",
                resp.status_code, resp.text[:200])
    call_log.record("uploadEmbeddingFile", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    _wire("uploadEmbeddingFile", "PUT", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}",
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
        resp = requests.post(url, json=payload, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        _wire("status", "POST", url, payload, error=str(exc),
              latency_ms=(time.perf_counter() - t0) * 1000)
        return False, None, str(exc)
    lat = (time.perf_counter() - t0) * 1000
    _wire("status", "POST", url, payload, status=resp.status_code,
          response=resp.text, latency_ms=lat)
    if resp.status_code < 400:
        return True, resp.status_code, None
    return False, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:120]}"
