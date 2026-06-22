from __future__ import annotations

"""
Thin client for the CERAVIS application server (Spring).

Only the calls the edge device needs. Everything goes through here so the
server address lives in ONE place (device config) and never in the browser.

Endpoint used during setup:
    POST {base}/v1/ai/userDetails   body {"email": "..."}
    -> ApiResponse<UserDetailsResponse>
       { ceravisUserId, firstName, lastName, email, gender, role, tier }
A user that exists in the server's DB is what "verifies" the operator; if the
email isn't found, onboarding is blocked.
"""

import logging
import re

import requests

from config.settings import settings


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
    # No auth token — the app server accepts the email lookup directly.
    return {"Content-Type": "application/json", "Accept": "application/json"}


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
    try:
        resp = requests.post(url, json={"email": email}, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("userDetails: cannot reach %s — %s", url, exc)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc

    logger.info("userDetails <- HTTP %s", resp.status_code)
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
    POST /v1/ai/saveCamera — register this patient's cameras with the app server.

    Body:
        { "patientUserId": <id>,
          "cameras": [ { device, model, supplier, room, url }, ... ] }
    `room` is the room name we label; `url` is the camera's WebSocket stream URL.
    Returns the server's response payload; raises CeravisApiError on failure.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/saveCamera"
    payload = {"patientUserId": patient_user_id, "cameras": cameras}
    logger.info("saveCamera -> PUT %s  patient=%s  cameras=%d  rooms=%s",
                url, patient_user_id, len(cameras), [c.get("room") for c in cameras])
    try:
        resp = requests.put(url, json=payload, headers=_headers(),
                            timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("saveCamera: cannot reach %s — %s", url, exc)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    logger.info("saveCamera <- HTTP %s  body=%s", resp.status_code, resp.text[:300])
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True


def save_alert(patient_user_id, alert_type: str, message_text: str):
    """
    POST /v1/ai/saveAlert — push one alert to the app server.
    Body: { patientUserId, alertType (e.g. "FALL"), messageText }.
    Returns the server's response; raises CeravisApiError on failure.
    """
    if not is_configured():
        raise CeravisApiError(
            "CERAVIS app server not configured (set CERAVIS_API_BASE_URL)")
    url = settings.ceravis_api_base_url.rstrip("/") + "/v1/ai/saveAlert"
    payload = {"patientUserId": patient_user_id,
               "alertType": alert_type, "messageText": message_text}
    logger.info("saveAlert -> POST %s  patient=%s  type=%s", url,
                patient_user_id, alert_type)
    try:
        resp = requests.post(url, json=payload, headers=_headers(),
                             timeout=settings.ceravis_api_timeout_secs)
    except requests.RequestException as exc:
        logger.warning("saveAlert: cannot reach %s — %s", url, exc)
        raise CeravisApiError(f"cannot reach app server: {exc}") from exc
    logger.info("saveAlert <- HTTP %s  body=%s", resp.status_code, resp.text[:300])
    if resp.status_code >= 400:
        raise CeravisApiError(
            f"app server returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return _unwrap(resp.json())
    except ValueError:
        return True
