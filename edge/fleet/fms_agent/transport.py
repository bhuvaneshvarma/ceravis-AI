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


def encode(payload) -> bytes:
    """The exact request-body bytes for `payload` (what is signed and what the
    server's size limit counts)."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def post(url: str, path: str, payload: dict, principal: str | None, secret: str,
         clock_offset: float) -> tuple[dict, float]:
    """principal None = enrollment: four signed lines, no device or nonce header."""
    body = encode(payload)                                  # sign these exact bytes
    stamp = f"{time.time() + clock_offset:.3f}"
    headers = {"Content-Type": "application/json", "User-Agent": "ceravis-fleet-agent",
               protocol.HDR_TIMESTAMP: stamp}
    if principal is None:
        message = protocol.enroll_request_message("POST", path, stamp, body)
        expect = lambda raw: protocol.enroll_response_message(stamp, raw)    # noqa: E731
    else:
        nonce = secrets.token_hex(16)
        message = protocol.request_message(principal, "POST", path, body, stamp, nonce)
        expect = lambda raw: protocol.response_message(principal, nonce, raw)  # noqa: E731
        headers.update({protocol.HDR_DEVICE: principal, protocol.HDR_NONCE: nonce})
    headers[protocol.HDR_SIGNATURE] = protocol.sign(secret, message)
    request = urllib.request.Request(url + path, data=body, method="POST", headers=headers)
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
    if not protocol.verify(secret, expect(raw), signature):
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
