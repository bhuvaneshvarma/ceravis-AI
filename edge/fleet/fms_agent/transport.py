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
    """The exchange failed; retrying later may fix it. `status` is the HTTP code (0 = none);
    `reply` is what the server answered to a heartbeat, as text, when it answered."""
    def __init__(self, message: str, status: int = 0, reply: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reply = reply



class UntrustedReply(Rejected):
    """The server answered, but its reply cannot be trusted or read: whatever it
    did on its side, this device must not act on the reply."""

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
        message = protocol.request_message("POST", path, stamp, nonce, body)
        expect = lambda raw: protocol.response_message(stamp, nonce, raw)      # noqa: E731
        headers.update({protocol.HDR_DEVICE: principal, protocol.HDR_NONCE: nonce})
    headers[protocol.HDR_SIGNATURE] = protocol.sign(secret, message)
    request = urllib.request.Request(url + path, data=body, method="POST", headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            raw, signature = resp.read(), resp.headers.get(protocol.HDR_SIGNATURE)
    except urllib.error.HTTPError as exc:
        answer = exc.read()
        detail = _json(answer)
        if exc.code == 401 and detail.get("error") == "clock_skew":
            raise ClockSkew(float(detail["server_time"])) from None
        reason = detail.get("error") or detail.get("detail") or exc.reason
        raise Rejected(f"HTTP {exc.code}: {reason}", exc.code, _answer(answer, principal)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise Rejected(f"cannot reach {url} ({getattr(exc, 'reason', exc)})") from None
    rtt_ms = round((time.monotonic() - started) * 1000, 1)
    if not protocol.verify(secret, expect(raw), signature):
        raise UntrustedReply("reply signature invalid — ignoring it", reply=_answer(raw, principal))
    reply = _json(raw)
    if not reply:
        raise UntrustedReply("reply was not valid JSON", reply=_answer(raw, principal))
    return reply, rtt_ms


ANSWER_CHARS = 2000      # of a failed heartbeat's answer, kept for the device's log


def _answer(raw: bytes | None, principal: str | None) -> str | None:
    """What the server answered, as text for the device's log (fleet.jsonl) — to a
    heartbeat only: an enrollment's answer carries the secret and is never kept."""
    if principal is None:
        return None
    text = (raw or b"").decode("utf-8", errors="replace")
    return text if len(text) <= ANSWER_CHARS else text[:ANSWER_CHARS] + f"…({len(text)} characters)"


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}
