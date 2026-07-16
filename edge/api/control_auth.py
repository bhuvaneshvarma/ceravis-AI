from __future__ import annotations

"""
One guard for the cloud-facing control endpoints (PTZ, recording playback)
reached through the frp tunnel. Two independent checks, both no-ops when
unconfigured so LAN development keeps working without ceremony:

  * control token — a shared secret the ceravishealth backend sends as the
    X-Ceravis-Control-Token header (settings.edge_control_token). Empty accepts
    everything (LAN dev only).
  * edge-id guard — obey only commands addressed to THIS device
    (settings.edge_id), so a misrouted request can never touch the wrong home.

Kept in one place so every control endpoint authenticates identically — see
[[ceravis-one-mechanism-principle]].
"""

from fastapi import HTTPException

from config.settings import settings


def check_control_token(header_value: str | None) -> None:
    """Raise 401 unless the shared control token matches (or none is set)."""
    secret = settings.edge_control_token.strip()
    if secret and (header_value or "").strip() != secret:
        raise HTTPException(401, "invalid or missing control token")


def check_edge_id(req_edge_id: str | None) -> None:
    """Raise 409 when a command is addressed to a different edge device."""
    mine = settings.edge_id.strip()
    req = (req_edge_id or "").strip()
    if mine and req and req != mine:
        raise HTTPException(409, f"edge_id mismatch: command for '{req}', "
                                 f"this device is '{mine}'")
