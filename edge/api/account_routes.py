from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from config.settings import settings
from configuration.account_config import AccountConfig, canonical_edge_id
from configuration.camera_config import CameraConfig
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError,
    get_user_details,
    is_configured,
    room_to_enum,
    save_cameras,
)
from livestream.mediamtx_client import is_up as media_backbone_up
from livestream.mediamtx_client import stream_base, sync_camera, webrtc_url


router = APIRouter(prefix="/api/v1/account", tags=["Account"])
account_config = AccountConfig()
logger = logging.getLogger("account")


def _stream_base(request: Request) -> str:
    """Externally-reachable base for the live links handed to the cloud —
    delegates to livestream.mediamtx_client.stream_base (the ONE base builder,
    shared with tests/test_cloud.py): DEVICE_STREAM_BASE override, else this
    host with the scheme MediaMTX is actually serving."""
    return stream_base(request.headers.get("host") or "localhost")


def _repoint_live_paths(request: Request) -> None:
    """After an edge_id change, bring the running device onto the new routing
    token WITHOUT a full restart: re-sync every camera's MediaMTX live path (now
    '<new_edge_id>/<ROOM>') and refresh the link stored in cameras.json so the
    next cloud sync pushes the current URL. Best-effort — a failure here just
    means a `systemctl restart ceravis` is needed to pick the change up."""
    import time
    time.sleep(1.5)                       # let the verify response flush first
    base = _stream_base(request)
    for c in CameraConfig().get_all():
        try:
            if media_backbone_up():
                sync_camera(c)
            c.webrtc_url = webrtc_url(c.camera_id, base)
            CameraConfig().update(c.camera_id, c)
        except Exception as exc:          # noqa: BLE001 — never fail provisioning
            logger.warning("repoint live path failed for %s: %s",
                           c.camera_id, exc)
    logger.info("live paths repointed to the new edge_id")


def _cloud_camera(c, base: str) -> dict:
    """One camera in the app-server saveCamera shape (camelCase) — the FULL
    cameras.json record, so the cloud mirrors the device exactly. `url` and
    `webrtcUrl` are ALWAYS computed fresh from the current edge_id (never the
    stored copy, which goes stale the moment the edge_id changes); `room` is
    normalized to the server CameraName enum (KITCHEN, LIVING_ROOM, …)."""
    link = webrtc_url(c.camera_id, base)
    return {
        # device/model/supplier come from the camera's ONVIF GetDeviceInformation
        # (filled on save). `supplier` carries the SERIAL number — the backend
        # renames that key to serialNumber later.
        "device": c.manufacturer or "",        # ONVIF manufacturer (e.g. Hikvision)
        "model": c.model or "",                # ONVIF model
        "supplier": c.serial or "",            # ONVIF serial number
        "room": room_to_enum(c.room_name),     # -> server CameraName enum
        "url": link,
        "rtspUrl": c.rtsp_url,
        "recordRtspUrl": c.record_rtsp_url or "",
        "hlsUrl": c.hls_url or "",
        "onvifXaddr": c.onvif_xaddr or "",
        "onvifUsername": c.onvif_username or "",
        "onvifPassword": c.onvif_password or "",
        "onvifProfileToken": c.onvif_profile_token or "",
        "onvifPtzToken": c.onvif_ptz_token or "",
        "ptzSupported": bool(c.ptz_supported),
        "isEnabled": bool(c.is_enabled),
        "webrtcUrl": link,
    }


class VerifyRequest(BaseModel):
    email: str
    phone: str | None = None


@router.post("/verify")
def verify(req: VerifyRequest, background_tasks: BackgroundTasks):
    """
    Gate the setup wizard: look the email up on the CERAVIS app server. If the
    account exists, persist it (with the entered phone) and return it so the next
    screen can pre-fill. The phone is stored alongside — UserDetailsResponse has
    no phone field, so the email is what the server verifies.
    """
    email = (req.email or "").strip()
    if not email:
        return {"verified": False, "reason": "Email is required"}
    try:
        user = get_user_details(email)
    except CeravisApiError as exc:
        logger.warning("account verify failed for %s: %s", email, exc)
        return {"verified": False, "reason": str(exc)}
    if not user:
        return {"verified": False,
                "reason": "No CERAVIS account found for this email"}

    # The app server hands this device's routing token back as `deviceToken`
    # (older field name: edgeId). Stored VERBATIM — the ONE canonical value the
    # live-link segment, cloud/frpc.toml locations, the MediaMTX path and control
    # auth all share. canonical_edge_id only trims/guards a path-safe token; it
    # never remaps characters, so the edge value can never diverge from the
    # backend's. See [[ceravis-devicetoken-verbatim]].
    raw_token = user.get("deviceToken") or user.get("edgeId")
    edge_id = canonical_edge_id(raw_token)
    prev_edge_id = account_config.get_edge_id()
    account = {
        "ceravisUserId": user.get("ceravisUserId"),
        "firstName": user.get("firstName"),
        "lastName": user.get("lastName"),
        "email": user.get("email") or email,
        "phone": (req.phone or "").strip(),
        "gender": user.get("gender"),
        "role": user.get("role"),
        "tier": user.get("tier"),
        "edgeId": edge_id or None,
    }
    account_config.save(account)
    # account.json is the authoritative runtime source (live links + control
    # checks read it with no restart — so ceravis itself never needs restarting).
    provisioning = False
    if edge_id:
        from config.env_writer import set_env_value
        set_env_value("EDGE_ID", edge_id)                # persist for the next boot
        # Apply to the frp tunnel (patch frpc.toml locations + restart frpc) AFTER
        # this response is sent, so we never cut the tunnel this reply travels
        # through. Best-effort: the value is already saved, so a manual frpc setup
        # still works if the privileged helper isn't installed.
        from integration.edge_provision import apply_edge_id_async
        background_tasks.add_task(apply_edge_id_async, edge_id)
        provisioning = True
        logger.info("edge_id provisioned: %s (jetson.env written; frpc apply "
                    "scheduled after response)", edge_id)
        # The edge_id is the first segment of every camera live path. When it
        # CHANGES, MediaMTX is still serving the old paths (its config was baked
        # at the last start) and cameras.json still holds the old links — so
        # repoint the live paths and refresh the stored links now. This makes the
        # device self-heal on re-provision with no `systemctl restart ceravis`.
        if edge_id != prev_edge_id:
            background_tasks.add_task(_repoint_live_paths, request)
            logger.info("edge_id changed %r -> %r: MediaMTX repoint + link "
                        "refresh scheduled", prev_edge_id, edge_id)
    logger.info("account verified: user #%s (%s)",
                account["ceravisUserId"], account["email"])
    return {"verified": True, "user": account, "edgeId": edge_id or None,
            "provisioning": provisioning}


@router.post("/sync-cameras")
def sync_cameras(request: Request):
    """
    Push every registered camera to the app server for the verified patient:
    PUT /v1/ai/saveCamera with patientUserId + the full per-camera record
    (device, model, supplier, room, url, rtspUrl, recordRtspUrl, hlsUrl,
    onvif*, ptzSupported, isEnabled, webrtcUrl — see _cloud_camera). `url`/
    `webrtcUrl` = the camera's WebRTC HTTPS live link (MediaMTX); the cloud
    mirrors exactly what cameras.json holds on the device.
    """
    acct = account_config.get()
    pid = acct.get("ceravisUserId")
    if not pid:
        return {"synced": False, "reason": "Account not verified — verify first"}
    cams = CameraConfig().get_all()
    if not cams:
        return {"synced": False, "reason": "No cameras registered yet"}

    base = _stream_base(request)
    # Fleet links route on the /<edge_id> path prefix; without EDGE_ID the pushed
    # URLs have no prefix and frp can't route them to this house.
    if settings.device_stream_base.strip() and not settings.edge_id.strip():
        logger.warning("sync-cameras: DEVICE_STREAM_BASE set but EDGE_ID empty — "
                       "fleet live links need the /<edge_id> prefix to route; set "
                       "EDGE_ID in jetson.env and re-sync")
        call_log.record(
            "event", False,
            label="Live links pushed without EDGE_ID — fleet routing will fail",
            error="set EDGE_ID (the /<edge_id> routing token) in jetson.env, "
                  "restart ceravis, then re-sync cameras")
    cameras = [_cloud_camera(c, base) for c in cams]
    logger.info("sync-cameras: pushing %d camera(s) for user #%s: %s",
                len(cameras), pid, [(c["room"], c["url"]) for c in cameras])

    # The links point at MediaMTX — if it isn't running they are dead on
    # arrival. Sync anyway (the server needs the cameras registered), but say
    # so loudly here AND on the monitor's sync console.
    backbone = media_backbone_up()
    if not backbone:
        logger.warning("sync-cameras: media backbone DOWN — pushed live links "
                       "will be dead until MediaMTX runs on this device")
        call_log.record(
            "event", False,
            label="Camera live links pushed while the media backbone is DOWN",
            error="links dead until MediaMTX runs — bash setup/"
                  "install_mediamtx.sh + restart ceravis, then re-sync cameras")

    try:
        result = save_cameras(pid, cameras)
    except CeravisApiError as exc:
        logger.warning("saveCamera failed (user #%s): %s", pid, exc)
        return {"synced": False, "reason": str(exc), "count": len(cameras)}
    logger.info("saveCamera: %d camera(s) sent for user #%s", len(cameras), pid)
    return {"synced": True, "count": len(cameras), "media_backbone": backbone,
            "patientUserId": pid, "cameras": cameras, "server": result}


@router.get("")
def get_account():
    """Return the stored verified account (so the wizard can resume) plus
    whether the app-server integration is configured on this device."""
    acct = account_config.get()
    return {
        "verified": bool(acct.get("ceravisUserId")),
        "user": acct or None,
        "configured": is_configured(),
    }
