from __future__ import annotations

"""
Auth for the cloud-facing control endpoints (PTZ, recording playback) reached
through the frp tunnel / Caddy.

Model (current):
  * The admin PAGES (/ui/*) are protected by HTTP Basic Auth at the cloud Caddy
    — humans get an id/password prompt.
  * The API endpoints (/api/*) are NOT behind Basic Auth (so the app server can
    call them freely); instead each control request must carry this device's
    `edgeId`, and we verify it MATCHES the edge_id provisioned for this device
    (from the userDetails response, stored in account.json / jetson.env). Only
    the app server that provisioned the device knows that value.

The legacy X-Ceravis-Control-Token was removed — check_control_token is now a
no-op kept only so existing call sites don't break. One place, so every control
endpoint authenticates identically — see [[ceravis-one-mechanism-principle]].
"""

from fastapi import HTTPException

from configuration.account_config import effective_edge_id


def check_control_token(header_value: str | None = None) -> None:
    """Deprecated no-op. The X-Ceravis-Control-Token was removed; the edgeId
    match (check_edge_id) is the API auth now. Retained so callers that still
    pass the old header don't error during the transition."""
    return


def check_edge_id(req_edge_id: str | None) -> None:
    """The primary control-endpoint auth: the request must target THIS device.

    When the device has an edge_id (a verified account, or jetson.env EDGE_ID),
    the request MUST carry a matching one — missing => 401, wrong => 409. A
    caller therefore needs the edge_id, which only the app server that
    provisioned this device knows. No edge_id on the device = LAN dev, so accept
    anything (nothing to route or protect yet)."""
    mine = effective_edge_id()
    if not mine:
        return
    req = (req_edge_id or "").strip()
    if not req:
        raise HTTPException(401, "edgeId required")
    if req != mine:
        raise HTTPException(409, f"edge_id mismatch: command for '{req}', "
                                 f"this device is '{mine}'")
