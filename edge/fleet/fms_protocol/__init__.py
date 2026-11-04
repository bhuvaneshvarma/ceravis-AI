"""The CERAVIS fleet wire protocol — the ONE definition both ends import.

The server (`fms`) and the device agent (`fms_agent`) must agree byte-for-byte
on how a request is signed and on which orders exist. Both live here, in one
stdlib-only module that also runs on the Jetson (Python 3.10+). The edge repo
carries a verbatim copy (tools/vendor_agent.py), so the two sides cannot drift.

Identity: a device IS its edge_id — permanent, fixed to the hardware. On first
contact the agent ENROLLS: it proves it runs the CERAVIS image (a request signed
with the fleet enrollment key) and presents its hardware fingerprint; the server
adopts the edge_id the device already has, or issues a new one, and returns the
device's own signing key. From then on every heartbeat is signed with that key.

Signing (HMAC-SHA256, Base64 — the same shape as the edge<->app-server design):

    request  = principal \\n METHOD \\n path \\n sha256hex(body) \\n timestamp \\n nonce
    response = principal \\n RESPONSE \\n request_nonce \\n sha256hex(body)

`principal` is the edge_id for heartbeats and the fingerprint for enrollment.
Replies are signed too, bound to the request's nonce, so a device only ever acts
on what the real server said to THIS request.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

VERSION = 1
HEARTBEAT_PATH = "/v1/device/heartbeat"
ENROLL_PATH = "/v1/device/enroll"

HDR_DEVICE = "X-Ceravis-Device"          # edge_id, or the fingerprint when enrolling
HDR_TIMESTAMP = "X-Ceravis-Timestamp"
HDR_NONCE = "X-Ceravis-Nonce"
HDR_SIGNATURE = "X-Ceravis-Signature"

MAX_BODY_BYTES = 512 * 1024          # a heartbeat is ~4 KB; anything near this is wrong
MAX_OUTPUT_CHARS = 64 * 1024         # cap on one command's returned output
MAX_RESULTS = 20                     # command results carried by one heartbeat

# An edge_id travels in URL paths (/<edge_id>/…) and as the frp SSH hostname, so
# only URL-safe characters qualify. It is VALIDATED, never rewritten: a value
# outside this set is not adopted at all (the server issues a fresh one instead).
EDGE_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")

# Exact systemd unit names (without ".service"). The agent's sudoers rule grants
# restart on RESTARTABLE_UNITS and nothing else; widening this list is a
# security decision, not a convenience.
RESTARTABLE_UNITS = ("ceravis", "frpc")
LOG_UNITS = ("ceravis", "frpc", "ceravis-fleet-agent")
MAX_LOG_LINES = 500

COMMANDS = {
    "ping": "Round-trip test — proves the device receives orders and answers.",
    "restart_service": "Restart one allow-listed service on the device.",
    "logs": "Return the latest journal lines of one allow-listed service.",
}


def valid_edge_id(value) -> bool:
    return isinstance(value, str) and bool(EDGE_ID_RE.match(value))


def _sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_message(principal: str, method: str, path: str, body: bytes,
                    timestamp: str, nonce: str) -> bytes:
    return "\n".join((principal, method.upper(), path, _sha256_hex(body),
                      timestamp, nonce)).encode("utf-8")


def response_message(principal: str, request_nonce: str, body: bytes) -> bytes:
    return "\n".join((principal, "RESPONSE", request_nonce,
                      _sha256_hex(body))).encode("utf-8")


def sign(secret: str, message: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def verify(secret: str, message: bytes, signature: str | None) -> bool:
    expected = sign(secret, message).encode("ascii")
    return hmac.compare_digest(expected, (signature or "").encode("utf-8", "replace"))


def normalize_command(kind: str, args: dict | None = None) -> dict:
    """Validate an order and return its canonical arguments; raise ValueError.

    The server calls this when an operator queues an order, and the agent calls
    it again before running one — so an order the server would refuse can never
    execute on a device, whatever reaches it."""
    args = dict(args or {})
    if kind == "ping":
        return {}
    if kind == "restart_service":
        return {"unit": _pick(args.get("unit"), RESTARTABLE_UNITS)}
    if kind == "logs":
        try:
            lines = int(args.get("lines", 200))
        except (TypeError, ValueError):
            raise ValueError("lines must be a whole number") from None
        return {"unit": _pick(args.get("unit"), LOG_UNITS),
                "lines": max(1, min(lines, MAX_LOG_LINES))}
    raise ValueError(f"unknown command {kind!r}")


def _pick(unit, allowed: tuple) -> str:
    if unit not in allowed:
        raise ValueError(f"unit must be one of: {', '.join(allowed)}")
    return unit
