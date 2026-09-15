"""One signed HTTPS round trip to the FMS, verified in both directions."""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request

import fms_protocol as protocol


class ClockSkew(Exception):
    """The server refused our timestamp; it told us its time so we can adjust."""
    def __init__(self, server_time: float) -> None:
        super().__init__("device clock is off")
        self.server_time = server_time


class Rejected(Exception):
    """The exchange failed; retrying later may fix it. `status` is the HTTP code (0 = none)."""
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def post(url: str, path: str, payload: dict, principal: str, secret: str,
         clock_offset: float) -> tuple[dict, float]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")   # sign these exact bytes
    stamp = f"{time.time() + clock_offset:.3f}"
    nonce = secrets.token_hex(16)
    message = protocol.request_message(principal, "POST", path, body, stamp, nonce)
    request = urllib.request.Request(url + path, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "ceravis-fleet-agent",
        protocol.HDR_DEVICE: principal,
        protocol.HDR_TIMESTAMP: stamp,
        protocol.HDR_NONCE: nonce,
        protocol.HDR_SIGNATURE: protocol.sign(secret, message),
    })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            raw, signature = resp.read(), resp.headers.get(protocol.HDR_SIGNATURE)
    except urllib.error.HTTPError as exc:
        detail = _json(exc.read())
        if exc.code == 401 and detail.get("error") == "clock_skew":
            raise ClockSkew(float(detail["server_time"])) from None
        reason = detail.get("error") or detail.get("detail") or exc.reason
        raise Rejected(f"HTTP {exc.code}: {reason}", exc.code) from None
    except (urllib.error.URLError, OSError) as exc:
        raise Rejected(f"cannot reach {url} ({getattr(exc, 'reason', exc)})") from None
    rtt_ms = round((time.monotonic() - started) * 1000, 1)
    if not protocol.verify(secret, protocol.response_message(principal, nonce, raw), signature):
        raise Rejected("reply signature invalid — ignoring it")
    reply = _json(raw)
    if not reply:
        raise Rejected("reply was not valid JSON")
    return reply, rtt_ms


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}
