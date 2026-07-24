from __future__ import annotations

"""
edge_id auth for the cloud-facing control endpoints (PTZ, recording playback).

Every control request carries this device's `edgeId`, and we verify it MATCHES
the edge_id provisioned for this device — the `deviceToken` from the userDetails
response, saved to account.json + jetson.env and also used for the live-link
first segment and the frp routing `locations`. Only the app server that
provisioned the device knows that value, so the match IS the authentication.

There is NO token here (the legacy X-Ceravis-Control-Token was removed for good):
the edge_id match is the single check. One place, so every control endpoint
authenticates identically — see [[ceravis-one-mechanism-principle]].
"""

from fastapi import HTTPException

from configuration.account_config import effective_edge_id


def check_edge_id(req_edge_id: str | None) -> None:
    """The request must target THIS device. When the device has an edge_id (a
    verified account / jetson.env EDGE_ID), the request MUST carry a matching
    one — missing => 401, wrong => 409. No edge_id on the device = LAN dev, so
    accept anything (nothing provisioned to check against yet)."""
    mine = effective_edge_id()
    if not mine:
        return
    req = (req_edge_id or "").strip()
    if not req:
        raise HTTPException(401, "edgeId required")
    if req != mine:
        raise HTTPException(409, f"edge_id mismatch: request for '{req}', "
                                 f"this device is '{mine}'")
