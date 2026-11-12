from __future__ import annotations

import logging
import re
import secrets
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from config.account_config import (
    AccountConfig, canonical_edge_id, effective_edge_id,
)
from config.camera_config import CameraConfig
from maintenance import recipient_reset
from integration.ceravis_api import (
    CeravisApiError,
    get_user_details,
    is_configured,
    room_to_enum,
    save_cameras,
    send_otp,
    verify_otp,
)


router = APIRouter(prefix="/api/v1/account", tags=["Account"])
account_config = AccountConfig()
logger = logging.getLogger("account")
# When this process started: a page that waits out a restart polls until it changes.
_BOOTED_AT = time.time()

# A sign-in by ANOTHER care recipient than the one on this device: their code
# checked out, but the device is only handed over once the person at the screen
# confirms that the current recipient's data is erased. Held in memory for a few
# minutes, by an unguessable token the sign-in page sends back with the yes.
_SWITCH_TTL_SECS = 600.0
_switches: dict[str, dict] = {}
_switch_lock = threading.Lock()


def _hold_switch(email: str, account: dict) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _switch_lock:
        for t in [t for t, v in _switches.items() if v["expires"] < now]:
            _switches.pop(t)
        _switches[token] = {"email": email.lower(), "account": account,
                            "expires": now + _SWITCH_TTL_SECS}
    return token


def _take_switch(token: str, email: str) -> dict | None:
    """The account held for this token and email (once), or None."""
    with _switch_lock:
        held = _switches.pop(token, None)
    if not held or held["expires"] < time.time() or held["email"] != email.lower():
        return None
    return held["account"]


def _hand_over(account: dict, actor: str, background_tasks: BackgroundTasks) -> None:
    """This device now serves `account`; whatever it held of the care recipient
    before is erased when the service restarts (maintenance.recipient_reset)."""
    account_config.clear()
    recipient_reset.request("handed over to another care recipient", actor)
    account_config.save(account)
    background_tasks.add_task(recipient_reset.restart_soon)


def _keep_tunnel_keyed(background_tasks: BackgroundTasks) -> tuple[bool, dict | None]:
    """Make sure the frp tunnel routes this device's edge_id (the FMS one).
    Re-applied only when it does not AND the privileged helper can do it —
    never on every login. Returns (provisioning, remote) for the login page:
    `provisioning` = a tunnel restart is really about to happen."""
    edge_id = effective_edge_id()
    if not edge_id:
        return False, None
    from integration.edge_provision import apply_edge_id_async, provisioning_report
    remote = provisioning_report(edge_id)
    if remote["helper_installed"] and not remote["keyed"]:
        background_tasks.add_task(apply_edge_id_async, edge_id)
        return True, remote
    return False, remote


def _note_app_server_token(user: dict) -> str:
    """The app server still names this device by its own token (deviceToken).
    It is kept in account.json only as the hint the fleet agent sends at
    enrollment; routing uses the FMS edge_id alone. When the two differ, say so
    plainly: the app server's links to this device miss until it uses the FMS
    edge_id. Returns the token ('' when none)."""
    token = canonical_edge_id((user or {}).get("deviceToken") or (user or {}).get("edgeId"))
    mine = effective_edge_id()
    if token and mine and token != mine:
        logger.warning("the app server names this device %s, the FMS edge_id is %s — "
                       "the app server must use %s", token, mine, mine)
        from integration import call_log
        call_log.record("event", False, label="App server uses another edge_id for this device",
                        error=f"app server: {token} — FMS: {mine}")
    return token


def _cloud_camera(c) -> dict:
    """One camera in the app-server saveCamera shape (camelCase). Every contract
    key is sent, but only the camera's identity and capabilities carry a value:
    device, model, supplier, room, ptzSupported, isEnabled. The rest never leave
    the device and go out empty (the backend made them optional): the stream and
    ONVIF addresses are LAN-only, so the cloud cannot use them and the CloudFront
    WAF rejects a body carrying an IP-address URL; the ONVIF credentials are
    secrets; the live link carries the edge_id. `room` is normalized to the
    server CameraName enum (KITCHEN, LIVING_ROOM, …)."""
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
        "ptzSupported": bool(c.ptz_supported),
        "isEnabled": bool(c.is_enabled),
        # Device-only — always sent empty (see the docstring).
        "url": "", "webrtcUrl": "", "hlsUrl": "",
        "rtspUrl": "", "recordRtspUrl": "",
        "onvifXaddr": "", "onvifUsername": "", "onvifPassword": "",
        "onvifProfileToken": "", "onvifPtzToken": "",
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
    # The yes to "erase the current care recipient's data": the token a
    # verified sign-in by another recipient was answered with (in place of otp).
    reset_token: str | None = None


def _account_from(user: dict, email: str, phone: str | None) -> dict:
    return {
        "ceravisUserId": user.get("ceravisUserId"),
        "firstName": user.get("firstName"),
        "lastName": user.get("lastName"),
        "email": user.get("email") or email,
        "phone": (phone or "").strip(),
        "gender": user.get("gender"),
        "role": user.get("role"),
        "tier": user.get("tier"),
        # The app server's token, kept only as the fleet agent's enrollment hint.
        "edgeId": _note_app_server_token(user) or None,
    }


def _signed_in_fresh(account: dict, background_tasks: BackgroundTasks,
                     restarting: bool) -> dict:
    provisioning, remote = _keep_tunnel_keyed(background_tasks)
    return {"verified": True, "user": account, "fresh": True,
            "edgeId": effective_edge_id() or None,
            "provisioning": provisioning, "remote": remote,
            "restarting": restarting, "bootedAt": _BOOTED_AT}


@router.post("/verify")
def verify(req: VerifyRequest, request: Request, background_tasks: BackgroundTasks):
    """
    Gate the setup wizard. First check the login one-time code with the app
    server (/v1/ai/verifyOtp). Its confirmation carries the account itself.
    Then EVERY sign-in reports this device to the server: /v1/ai/userDetails
    with {email, deviceId}, so the server maps the device (its FMS record in
    the shared DB) to the account; its account answer is used only when the
    confirmation has none. Persist the account and
    return it so the next screen can pre-fill. The edge_id is never taken from
    here: the FMS issued it to this device (config.account_config). Every
    app-server hit is recorded in the wire log.

    ANOTHER care recipient than the one this device serves is not let in at
    once: the answer asks to confirm (`confirmReset` + `resetToken`), and only
    the same email sent back with that token hands the device over — the
    current recipient's data is erased (maintenance.recipient_reset).
    """
    email = (req.email or "").strip()
    if not email:
        return {"verified": False, "reason": "Email is required"}
    if req.reset_token:
        account = _take_switch(req.reset_token, email)
        if account is None:
            return {"verified": False,
                    "reason": "That confirmation has expired — sign in again."}
        _hand_over(account, email, background_tasks)
        logger.warning("device handed over to user #%s (%s); the previous care "
                       "recipient's data is erased on restart",
                       account["ceravisUserId"], account["email"])
        return _signed_in_fresh(account, background_tasks, restarting=True)
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

    # 2) Tell the app server which device this sign-in happens on (userDetails
    # carries this device's deviceId). A report: a failure here never blocks a
    # sign-in the confirmation (or the stored account) already allows.
    verified = verified or {}
    try:
        details, details_error = get_user_details(email), None
    except CeravisApiError as exc:
        details, details_error = None, exc
        logger.warning("userDetails failed for %s (sign-in continues): %s", email, exc)

    # 3) RETURNING device — account.json already holds THIS recipient. The local
    # account stays the source of truth (userDetails above only reported the
    # device; its answer does not overwrite it). We only make sure the frp
    # tunnel is still keyed to this device's edge_id, then let the operator
    # straight into the app. `fresh:false` tells the login page to open the
    # Live Wall (not the onboarding wizard). The edge_id is the FMS's, so what
    # the app server calls this device never changes the route.
    stored = account_config.get()
    if (stored.get("ceravisUserId")
            and (stored.get("email") or "").strip().lower() == email.lower()):
        _note_app_server_token(verified)
        provisioning, remote = _keep_tunnel_keyed(background_tasks)
        logger.info("returning login: user #%s (%s)", stored.get("ceravisUserId"), email)
        return {"verified": True, "user": stored, "fresh": False,
                "edgeId": effective_edge_id() or None, "provisioning": provisioning,
                "remote": remote}

    # 4) FRESH registration (no local account yet, or a different email) — the
    # account the server confirmed with the code, else the userDetails answer,
    # and onboard via the wizard (`fresh:true`).
    user = verified if verified.get("ceravisUserId") else details
    if not user and details_error is not None:
        return {"verified": False, "reason": _login_error(details_error)}
    if not user:
        return {"verified": False,
                "reason": "No CERAVIS account found for this email"}

    account = _account_from(user, email, req.phone)
    # This device serves another care recipient: hand it over only on a yes.
    if stored.get("ceravisUserId") and stored.get("ceravisUserId") != account["ceravisUserId"]:
        logger.info("sign-in by user #%s while the device serves user #%s — asking to "
                    "confirm the hand-over", account["ceravisUserId"], stored.get("ceravisUserId"))
        return {"verified": False, "confirmReset": True,
                "resetToken": _hold_switch(email, account),
                "erased": recipient_reset.erased_labels(),
                "kept": list(recipient_reset.KEPT),
                "reason": "This device is set up for another care recipient."}
    account_config.save(account)
    logger.info("account verified (fresh): user #%s (%s)",
                account["ceravisUserId"], account["email"])
    return _signed_in_fresh(account, background_tasks, restarting=False)


@router.get("/recipient-data")
def recipient_data():
    """What this device holds of its care recipient — what removing them erases —
    and what stays."""
    acct = account_config.get()
    name = " ".join(str(x) for x in (acct.get("firstName"), acct.get("lastName")) if x)
    return {"recipient": ({"ceravisUserId": acct.get("ceravisUserId"), "name": name,
                           "email": acct.get("email")} if acct.get("ceravisUserId") else None),
            "parts": recipient_reset.inventory(),
            "kept": list(recipient_reset.KEPT),
            "pending": recipient_reset.pending() is not None}


class RemoveRecipientRequest(BaseModel):
    confirm: bool = False


@router.post("/remove-recipient")
def remove_recipient(req: RemoveRecipientRequest, request: Request,
                     background_tasks: BackgroundTasks):
    """Remove the care recipient from this device before it goes to another
    one: the account at once (every browser is signed out with it), the rest of
    their data when the service restarts (maintenance.recipient_reset). The
    device's cameras, fleet enrollment and settings stay. Needs {confirm: true}."""
    if req.confirm is not True:
        return {"removed": False, "reason": "Confirmation required"}
    from api.auth import session_of
    who = (session_of(request) or {}).get("email") or "this device"
    acct = account_config.get()
    account_config.clear()
    recipient_reset.request("care recipient removed", who)
    background_tasks.add_task(recipient_reset.restart_soon)
    logger.warning("care recipient user #%s removed from this device by %s",
                   acct.get("ceravisUserId"), who)
    return {"removed": True, "restarting": True, "bootedAt": _BOOTED_AT}


@router.post("/sync-cameras")
def sync_cameras(camera_id: str | None = None):
    """
    Push registered camera(s) to the app server for the verified patient:
    PUT /v1/ai/saveCamera with patientUserId + one record per camera carrying
    its identity and capabilities only (device, model, supplier, room,
    ptzSupported, isEnabled); addresses, credentials and the live link stay on
    the device — see _cloud_camera.

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

    cameras = [_cloud_camera(c) for c in cams]
    logger.info("sync-cameras: pushing %d camera(s) for user #%s: %s",
                len(cameras), pid, [(c["device"], c["room"]) for c in cameras])

    try:
        result = save_cameras(pid, cameras)
    except CeravisApiError as exc:
        logger.warning("saveCamera failed (user #%s): %s", pid, exc)
        return {"synced": False, "reason": str(exc), "count": len(cameras)}
    logger.info("saveCamera: %d camera(s) sent for user #%s", len(cameras), pid)
    return {"synced": True, "count": len(cameras),
            "patientUserId": pid, "cameras": cameras, "server": result}


@router.get("")
def get_account(request: Request):
    """Return the stored verified account (so the wizard can resume) plus
    whether the app-server integration is configured on this device.

    `edge_id` is the CANONICAL routing token — the same value the edge serves
    its MediaMTX paths under and frp routes on. LAN-served UI pages read it to
    build a camera's live-stream path; through the tunnel they already know it
    (it is the URL prefix) and never ask.

    Not signed in: only whether an account exists, whether the server is set up
    and when the service started (the sign-in page waits on that across a
    restart) — never the account's email, details or edge_id."""
    acct = account_config.get()
    from api.auth import signed_in
    if not signed_in(request):
        return {"verified": bool(acct.get("ceravisUserId")), "user": None,
                "configured": is_configured(), "bootedAt": _BOOTED_AT}
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
