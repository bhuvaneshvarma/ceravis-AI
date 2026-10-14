from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from config.settings import settings
from configuration.account_config import (
    AccountConfig, canonical_edge_id, effective_edge_id,
)
from configuration.camera_config import CameraConfig
from integration import call_log
from integration.ceravis_api import (
    CeravisApiError,
    get_user_details,
    is_configured,
    room_to_enum,
    save_cameras,
    send_otp,
    verify_otp,
)
from livestream.mediamtx_client import is_up as media_backbone_up
from livestream.mediamtx_client import (
    drop_previous_live_path, stream_base, sync_camera, webrtc_url,
)


router = APIRouter(prefix="/api/v1/account", tags=["Account"])
account_config = AccountConfig()
logger = logging.getLogger("account")
# When this process started: a page that waits out a restart polls until it changes.
_BOOTED_AT = time.time()


def _stream_base(request: Request) -> str:
    """Externally-reachable base for the live links handed to the cloud —
    delegates to livestream.mediamtx_client.stream_base (the ONE base builder,
    shared with tests/test_cloud.py): DEVICE_STREAM_BASE override, else this
    host with the scheme MediaMTX is actually serving."""
    return stream_base(request.headers.get("host") or "localhost")


def _repoint_live_paths(base: str, previous_edge_id: str, cameras) -> None:
    """After an edge_id change, bring the running device onto the new routing
    token WITHOUT a full restart, per camera:
      1. drop the live path under the PREVIOUS token — left in place it keeps a
         second connection to the camera;
      2. sync the live path under the new token ('<new_edge_id>/<ROOM>');
      3. refresh the link stored in cameras.json, so the next cloud sync pushes
         the current URL;
      4. restart the running AI reader whose loopback URL carried the old token
         (a camera with its own '<cam>-ai' path never changed, so it is left be).
    `base` is the scheme://host live-link base and `cameras` the CameraManager
    (None when the pipeline is not up). Best-effort — a failure here just means
    a `systemctl restart ceravis` is needed to pick the change up."""
    time.sleep(1.5)                       # let the verify response flush first
    running = cameras.get_status() if cameras is not None else {}
    for c in CameraConfig().get_all():
        try:
            if media_backbone_up():
                drop_previous_live_path(c.camera_id, previous_edge_id)
                sync_camera(c)
            c.webrtc_url = webrtc_url(c.camera_id, base)
            CameraConfig().update(c.camera_id, c)
            status = running.get(c.camera_id)
            if status is not None and status.is_running and not getattr(c, "ai_rtsp_url", ""):
                cameras.restart_camera(c.camera_id)
        except Exception as exc:          # noqa: BLE001 — never fail provisioning
            logger.warning("repoint live path failed for %s: %s",
                           c.camera_id, exc)
    logger.info("live paths repointed to the new edge_id")


def _restart_after_switch() -> None:
    """A care-recipient switch changes who the whole device works for: the
    edge_id, the recipient's galleries, every live path and cloud link. The
    steps queued before this one (frpc, live paths) apply in place first; then
    the service restarts cleanly through the nightly refresh's own mechanism, so
    every component starts again on the new recipient at once, not at the next
    manual or overnight restart. Not under systemd (dev box): logged, and the
    in-place steps stand on their own."""
    from maintenance.refresh import restart_service
    time.sleep(1.0)
    logger.warning("care-recipient switch applied — restarting the service so "
                   "everything runs on the new recipient")
    if not restart_service():
        logger.warning("service restart NOT signalled (not running under systemd); "
                       "the in-place updates stand")


def _cloud_camera(c, base: str) -> dict:
    """One camera in the app-server saveCamera shape (camelCase) — the FULL
    cameras.json record, so the cloud mirrors the device exactly. `url` and
    `webrtcUrl` are ALWAYS computed fresh from the current edge_id (never the
    stored copy, which goes stale the moment the edge_id changes); `room` is
    normalized to the server CameraName enum (KITCHEN, LIVING_ROOM, …)."""
    link = webrtc_url(c.camera_id, base)
    return {
        # `device` is this camera's STABLE UNIQUE identity on the device —
        # CAM_01, CAM_02, ... — allocated once by CameraConfig and never reused.
        # It used to carry the ONVIF manufacturer, which is the same string on
        # every camera in a house ("tp-link") and so could not tell two of them
        # apart; the make is still legible from `model`. The camera_id fallback
        # (the ROOM) only fires if a record somehow escaped label allocation, so
        # this key is never empty and never duplicated across a house.
        # model/supplier still come from ONVIF GetDeviceInformation, filled on
        # save. `supplier` carries the SERIAL number — the backend renames that
        # key to serialNumber later.
        "device": c.device_label or c.camera_id,
        "model": c.model or "",                # ONVIF model
        "supplier": c.serial or "",            # ONVIF serial number
        "room": room_to_enum(c.room_name),     # -> server CameraName enum
        "url": link,
        "rtspUrl": c.rtsp_url,
        # Reserved: the contract keeps the key, and it is always empty because
        # the main stream above is what gets recorded (no second stream exists).
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


class RequestOtpRequest(BaseModel):
    email: str


def _login_error(exc: CeravisApiError) -> str:
    """The login screen's words for an app-server call that could not be made:
    a plain retry message when the server is unreachable or broken (5xx), the
    error itself otherwise (e.g. the server is not configured on this device)."""
    if not is_configured() or (exc.status is not None and exc.status < 500):
        return str(exc)
    if exc.status is None:
        return ("Couldn't reach the CERAVIS server. Check this device's internet "
                "connection and try again.")
    return "The CERAVIS server couldn't handle this right now. Please try again in a moment."


@router.post("/request-otp")
def request_otp(req: RequestOtpRequest):
    """Kick off the login one-time-code for a care recipient signing in on this
    edge device: POST the email to app.ceravishealth `/v1/ai/sendOtp`, which
    emails a 5-digit code. Returns {"sent": True} only when the server accepts it
    (the account exists); {"sent": False, reason} otherwise — the server's own
    message when it gives one — so the UI knows whether to advance to the code
    screen. The full exchange lands in the wire log.
    """
    email = (req.email or "").strip()
    if not email:
        return {"sent": False, "reason": "Email is required"}
    try:
        ok, reason = send_otp(email)
    except CeravisApiError as exc:
        logger.warning("sendOtp failed for %s: %s", email, exc)
        return {"sent": False, "reason": _login_error(exc)}
    if not ok:
        logger.info("sendOtp refused for %s: %s", email, reason)
        return {"sent": False, "reason": reason}
    logger.info("login OTP sent to %s", email)
    return {"sent": True, "email": email}


class VerifyRequest(BaseModel):
    email: str
    phone: str | None = None
    # The 5-digit login code the recipient typed; checked with the app server
    # (/v1/ai/verifyOtp) BEFORE the account is fetched.
    otp: str | None = None


@router.post("/verify")
def verify(req: VerifyRequest, request: Request, background_tasks: BackgroundTasks):
    """
    Gate the setup wizard. First check the login one-time code with the app
    server (/v1/ai/verifyOtp). Its confirmation carries the account itself
    (deviceToken = this device's edge_id); /v1/ai/userDetails is asked only when
    it does not. Persist the account and return it so the next screen can
    pre-fill. Every app-server hit is recorded in the wire log.
    """
    email = (req.email or "").strip()
    if not email:
        return {"verified": False, "reason": "Email is required"}
    # 1) The one-time code must check out before we fetch anything — and only
    # a well-formed code is worth asking the server about.
    otp = re.sub(r"\s+", "", req.otp or "")
    if not re.fullmatch(r"[0-9]{5}", otp):
        return {"verified": False, "reason": "Enter the 5-digit code from your email."}
    try:
        ok, reason, verified = verify_otp(email, otp)
    except CeravisApiError as exc:
        logger.warning("verifyOtp failed for %s: %s", email, exc)
        return {"verified": False, "reason": _login_error(exc)}
    if not ok:
        logger.info("login code not accepted for %s: %s", email, reason)
        return {"verified": False, "reason": reason}

    # 2) RETURNING device — account.json already holds THIS recipient with the
    # SAME edge_id the server just confirmed. A normal sign-in (to add a camera /
    # edit zones) must NOT re-fetch userDetails every time; the local account is
    # the source of truth. We only make sure the frp tunnel is still keyed to the
    # stored edge_id, then let the operator straight into the app. `fresh:false`
    # tells the login page to open the Live Wall (not the onboarding wizard).
    # A DIFFERENT edge_id for the same recipient (e.g. the device moved to
    # another app server) is not a returning login: it goes through step 3, so
    # the new edge_id is saved and applied exactly like a new recipient's.
    verified = verified or {}
    server_edge_id = canonical_edge_id(verified.get("deviceToken") or verified.get("edgeId"))
    stored = account_config.get()
    if (stored.get("ceravisUserId")
            and (stored.get("email") or "").strip().lower() == email.lower()
            and server_edge_id in ("", account_config.get_edge_id())):
        edge_id = account_config.get_edge_id()
        provisioning, remote = False, None
        if edge_id:
            from integration.edge_provision import (
                apply_edge_id_async, provisioning_report,
            )
            remote = provisioning_report(edge_id)
            # Re-apply only when the tunnel is not already routing this token AND
            # the privileged helper is present to do it — never on every login.
            if remote["helper_installed"] and not remote["keyed"]:
                background_tasks.add_task(apply_edge_id_async, edge_id)
                provisioning = True
        logger.info("returning login: user #%s (%s) — userDetails skipped",
                    stored.get("ceravisUserId"), email)
        return {"verified": True, "user": stored, "fresh": False,
                "edgeId": edge_id or None, "provisioning": provisioning,
                "remote": remote}

    # 3) FRESH registration (no local account yet, a different email, or a new
    # edge_id) — the account the server confirmed with the code, else fetched
    # from userDetails, and onboard via the wizard (`fresh:true`).
    try:
        user = verified if verified.get("ceravisUserId") else get_user_details(email)
    except CeravisApiError as exc:
        logger.warning("account verify failed for %s: %s", email, exc)
        return {"verified": False, "reason": _login_error(exc)}
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
    remote = None
    if edge_id:
        # NOT written into jetson.env. account.json already persists it, is
        # gitignored, and effective_edge_id() reads it FIRST — so copying the
        # token into a git-TRACKED file bought nothing and cost a merge conflict
        # on every device at every update, with `git checkout --` as the usual
        # "fix" silently discarding the routing token. One home for the value.
        # Apply to the frp tunnel (patch frpc.toml locations + restart frpc) AFTER
        # this response is sent, so we never cut the tunnel this reply travels
        # through. Best-effort: the value is already saved, so a manual frpc setup
        # still works if the privileged helper isn't installed.
        from integration.edge_provision import (
            apply_edge_id_async, provisioning_report,
        )
        background_tasks.add_task(apply_edge_id_async, edge_id)
        remote = provisioning_report(edge_id)
        # `provisioning` drives the UI's "restarting the tunnel…" spinner + the
        # reconnect wait, so it must mean a tunnel restart is ACTUALLY going to
        # happen — i.e. the privileged helper is installed. Reporting True on a
        # device without the helper made the UI promise "remote access ready"
        # while the auto-apply silently no-op'd and the tunnel stayed on the old
        # token. Now the UI reflects the honest state (and shows the manual
        # command from `remote` when the helper is missing).
        provisioning = remote["helper_installed"]
        if provisioning:
            logger.info("edge_id %s saved to account.json; frpc apply scheduled "
                        "after response (NOT written to jetson.env by design)",
                        edge_id)
        else:
            logger.warning("edge_id %s saved, but the privileged frpc helper is "
                           "NOT installed — the tunnel will not auto-update. Run "
                           "'%s' then '%s'", edge_id,
                           remote["install_cmd"], remote["apply_cmd"])
        # The edge_id is the first segment of every camera live path. When it
        # CHANGES, MediaMTX is still serving the old paths (its config was baked
        # at the last start) and cameras.json still holds the old links — so
        # repoint the live paths and refresh the stored links now. This makes the
        # device self-heal on re-provision with no `systemctl restart ceravis`.
        if edge_id != prev_edge_id:
            # Compute the link base NOW (the Request is valid inside the handler);
            # the background task only needs the resulting scheme://host string.
            background_tasks.add_task(
                _repoint_live_paths, _stream_base(request), prev_edge_id,
                getattr(request.app.state, "camera_manager", None))
            logger.info("edge_id changed %r -> %r: MediaMTX repoint + link "
                        "refresh scheduled", prev_edge_id, edge_id)
    # A different recipient or edge_id: restart once the steps above have run
    # (BackgroundTasks run in order), so nothing keeps the previous identity.
    restarting = (edge_id != prev_edge_id
                  or stored.get("ceravisUserId") != account["ceravisUserId"])
    if restarting:
        background_tasks.add_task(_restart_after_switch)
    logger.info("account verified (fresh): user #%s (%s)%s",
                account["ceravisUserId"], account["email"],
                " — service restart scheduled" if restarting else "")
    return {"verified": True, "user": account, "fresh": True,
            "edgeId": edge_id or None,
            "provisioning": provisioning, "remote": remote,
            "restarting": restarting, "bootedAt": _BOOTED_AT}


@router.post("/sync-cameras")
def sync_cameras(request: Request, camera_id: str | None = None):
    """
    Push registered camera(s) to the app server for the verified patient:
    PUT /v1/ai/saveCamera with patientUserId + the full per-camera record
    (device, model, supplier, room, url, rtspUrl, recordRtspUrl, hlsUrl,
    onvif*, ptzSupported, isEnabled, webrtcUrl — see _cloud_camera). `url`/
    `webrtcUrl` = the camera's WebRTC HTTPS live link (MediaMTX); the cloud
    mirrors exactly what cameras.json holds on the device.

    `camera_id` (optional) pushes ONLY that one camera — used when a single new
    camera is added on cameras.html, so the server isn't re-sent cameras it
    already has (which it rejects as duplicates). The setup wizard's Continue
    omits it to push the whole first-time batch.
    """
    acct = account_config.get()
    pid = acct.get("ceravisUserId")
    if not pid:
        return {"synced": False, "reason": "Account not verified — verify first"}
    cams = CameraConfig().get_all()
    if camera_id:
        cams = [c for c in cams if c.camera_id == camera_id]
        if not cams:
            return {"synced": False, "reason": f"Camera {camera_id} not found"}
    if not cams:
        return {"synced": False, "reason": "No cameras registered yet"}

    base = _stream_base(request)
    # Fleet links route on the /<edge_id> path prefix; with no routing token the
    # pushed URLs have no prefix and frp can't route them to this house. Resolve
    # the token the ONE canonical way (account.json first, jetson.env fallback) —
    # reading settings.edge_id here fired this warning on EVERY fleet sync after a
    # verify, because the verified token lands in account.json, never jetson.env.
    if settings.device_stream_base.strip() and not effective_edge_id():
        logger.warning("sync-cameras: DEVICE_STREAM_BASE set but no edge_id — "
                       "fleet live links need the /<edge_id> prefix to route; "
                       "verify the account (or set EDGE_ID) and re-sync")
        call_log.record(
            "event", False,
            label="Live links pushed without an edge_id — fleet routing will fail",
            error="verify the CERAVIS account so the device receives its "
                  "deviceToken (or set EDGE_ID in jetson.env), then re-sync cameras")
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
    whether the app-server integration is configured on this device.

    `edge_id` is the CANONICAL routing token — the same value the edge serves
    its MediaMTX paths under and frp routes on. LAN-served UI pages read it to
    build a camera's live-stream path; through the tunnel they already know it
    (it is the URL prefix) and never ask."""
    acct = account_config.get()
    # `remote` is the SAME provisioning summary the verify response carries, read
    # live from frpc.toml — so after the background apply runs, the wizard's
    # reconnect poll can confirm the tunnel is REALLY keyed (remote.keyed) rather
    # than assuming success, and show the manual command if it isn't.
    from integration.edge_provision import provisioning_report
    return {
        "verified": bool(acct.get("ceravisUserId")),
        "user": acct or None,
        "edge_id": effective_edge_id(),
        "configured": is_configured(),
        "remote": provisioning_report(),
        "bootedAt": _BOOTED_AT,
    }
