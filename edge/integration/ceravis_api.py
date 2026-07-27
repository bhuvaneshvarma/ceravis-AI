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
import logging
import re
import time

import requests

from config.settings import settings
from integration import call_log


logger = logging.getLogger("integration")


# The app server's fixed CameraName enum — `room` must be one of these exactly.
CAMERA_ROOM_ENUM = ("KITCHEN", "BEDROOM", "LIVING_ROOM", "LOUNGE", "LIVE_FEED")


def room_to_enum(room_name: str) -> str:
    """Normalize a saved room label to the server's CameraName enum, e.g.
    'Living Room' -> 'LIVING_ROOM'. Anything that doesn't match a known room
    falls back to LIVE_FEED so the call never fails on an unexpected label."""
    key = re.sub(r"[^A-Z0-9]+", "_", (room_name or "").upper()).strip("_")
    return key if key in CAMERA_ROOM_ENUM else "LIVE_FEED"


class CeravisApiError(Exception):
    """Transport / configuration / server error talking to the app server."""


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
    logger.info("userDetails -> POST %s  email=%s", url, email)
    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json={"email": email}, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("userDetails: cannot reach %s — %s", url, exc)
        call_log.record("userDetails", False, label=email, error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc

    lat = (time.perf_counter() - t0) * 1000
    logger.info("userDetails <- HTTP %s", resp.status_code)
    call_log.record("userDetails", resp.status_code < 400, label=email,
                    status=resp.status_code, latency_ms=lat)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning("userDetails error body: %s", resp.text[:300])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}")
    try:
        data = _unwrap(resp.json())
    except ValueError as exc:
        raise CeravisApiError("app server returned non-JSON") from exc
    if not isinstance(data, dict) or not data.get("ceravisUserId"):
        return None                              # account not found / empty
    return data


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
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveCamera <- HTTP %s  body=%s", resp.status_code, resp.text[:300])
    call_log.record("saveCamera", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
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
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveAlert <- HTTP %s  body=%s", resp.status_code, resp.text[:300])
    if resp.status_code >= 400:
        call_log.record("saveAlert", False, label=label,
                        status=resp.status_code, latency_ms=lat,
                        error=resp.text[:200])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        result = _unwrap(resp.json())
    except ValueError:
        result = True
    call_log.record("saveAlert", True, label=label, status=resp.status_code,
                    latency_ms=lat, alert_id=alert_id_of(result))
    return result


def save_snapshot(patient_id, base64_image: str, text: str, camera_number: str,
                  alert_id: int | None = None):
    """
    POST /v1/ai/saveSnapshot — one snapshot tied to an alert/action.
    Body: { patientId, base64Image, text, cameraNumber, alertId }.
      base64Image : the JPEG, base64-encoded (no data-URI prefix)
      text        : the same labeled line the alert carries
      cameraNumber: the room CameraName enum (KITCHEN, LIVING_ROOM, …)
      alertId     : the id returned by saveAlert, linking this snapshot to its
                    alert. Omitted for snapshot-only events (transitions) that
                    have no originating alert.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/saveSnapshot"
    payload = {"patientId": patient_id, "base64Image": base64_image,
               "text": text, "cameraNumber": camera_number}
    if alert_id is not None:
        payload["alertId"] = alert_id
    logger.info("saveSnapshot -> POST %s  patient=%s  cam=%s  alert=%s  img_b64=%d bytes",
                url, patient_id, camera_number, alert_id, len(base64_image or ""))
    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json=payload, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("saveSnapshot: cannot reach %s — %s", url, exc)
        call_log.record("saveSnapshot", False, label=text, alert_id=alert_id,
                        error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("saveSnapshot <- HTTP %s  body=%s", resp.status_code, resp.text[:200])
    if resp.status_code >= 400:
        call_log.record("saveSnapshot", False, label=text, alert_id=alert_id,
                        status=resp.status_code, latency_ms=lat,
                        error=resp.text[:200])
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
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
    POST /v1/ai/getPatientPostures — the patient's enrollment posture images
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
    logger.info("getPatientPostures -> POST %s  user=%s", url, user_id)
    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json={"userId": user_id}, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("getPatientPostures: cannot reach %s — %s", url, exc)
        call_log.record("getPatientPostures", False, label=str(user_id),
                        error=str(exc),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("getPatientPostures <- HTTP %s", resp.status_code)
    call_log.record("getPatientPostures", resp.status_code < 400,
                    label=str(user_id), status=resp.status_code, latency_ms=lat)
    if resp.status_code == 404:
        return []
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
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
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    lat = (time.perf_counter() - t0) * 1000
    logger.info("uploadEmbeddingFile <- HTTP %s  body=%s",
                resp.status_code, resp.text[:200])
    call_log.record("uploadEmbeddingFile", resp.status_code < 400, label=label,
                    status=resp.status_code, latency_ms=lat)
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True
