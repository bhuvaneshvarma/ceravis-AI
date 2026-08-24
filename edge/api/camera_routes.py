from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict

import cv2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api import ptz_control
from api.control_auth import canon, check_edge_id, field
from common import clock
from common.rtsp import normalize_rtsp_url
from configuration.account_config import effective_edge_id
from configuration.camera_config import CameraConfig
from ingestion.camera_manager import CameraManager
from integration import call_log
from livestream import mediamtx_client
from livestream.mediamtx_client import MediaMTXError
from schemas.cameras import Camera


router = APIRouter(
    prefix="/api/v1/cameras",
    tags=["Cameras"],
)

camera_config = CameraConfig()
logger = logging.getLogger("cameras")


def _mgr(request: Request) -> CameraManager:
    return request.app.state.camera_manager


def _mtx_sync(request: Request, camera: Camera) -> None:
    """Mirror a camera create/update into its MediaMTX path (best-effort:
    a media-backbone hiccup must not fail the camera save)."""
    if not getattr(request.app.state, "mediamtx_active", False):
        return
    try:
        mediamtx_client.sync_camera(camera)
    except MediaMTXError as exc:
        logger.warning("MediaMTX path sync failed for %s: %s",
                       camera.camera_id, exc)


def _mtx_remove(request: Request, camera_id: str) -> None:
    if not getattr(request.app.state, "mediamtx_active", False):
        return
    try:
        mediamtx_client.remove_camera(camera_id)
    except MediaMTXError as exc:
        logger.warning("MediaMTX path remove failed for %s: %s", camera_id, exc)


# =====================================================================
# STATIC PATHS FIRST (required so they don't collide with /{camera_id})
# =====================================================================

@router.get("")
def list_cameras():
    return camera_config.get_all()


def _normalize_urls(camera: Camera) -> None:
    """Credential-encode the camera's RTSP URLs before they're stored or handed to
    MediaMTX, so a password with '@' (or an operator who un-encoded %40 by hand)
    can't produce a URL that FFmpeg mis-parses. Idempotent."""
    camera.rtsp_url = normalize_rtsp_url(camera.rtsp_url)
    if camera.ai_rtsp_url:
        camera.ai_rtsp_url = normalize_rtsp_url(camera.ai_rtsp_url)


def _enrich_device_info(camera: Camera) -> None:
    """Best-effort: fill the camera's hardware descriptors from its ONVIF
    GetDeviceInformation when blank and ONVIF creds exist — so the saveCamera
    record carries the real make/model/serial without the operator typing them.
    Runs ONLY when something is missing (once filled, a re-save skips the SOAP
    call) and NEVER fails the save: a manual camera (no ONVIF) or one unreachable
    right now just keeps the blank fields.

    Cloud mapping (see account_routes._cloud_camera): device <- manufacturer,
    model <- model, supplier <- serial."""
    if not camera.onvif_xaddr or (camera.manufacturer and camera.model
                                  and camera.serial):
        return
    try:
        from onvif.client import OnvifCamera
        info = OnvifCamera(camera.onvif_xaddr, camera.onvif_username or "",
                           camera.onvif_password or "").device_info()
        camera.manufacturer = camera.manufacturer or info.get("manufacturer", "")
        camera.model = camera.model or info.get("model", "")
        camera.serial = camera.serial or info.get("serial", "")
        logger.info("device-info enriched %s: manufacturer=%r model=%r serial=%r",
                    camera.camera_id, camera.manufacturer, camera.model,
                    camera.serial)
    except Exception as exc:                      # noqa: BLE001 — never fail a save
        logger.info("device-info enrich skipped for %s: %s",
                    camera.camera_id, exc)


def _set_links(request: Request, camera: Camera) -> None:
    """Compute + store the camera's public live link so cameras.json and the
    app-server sync share ONE canonical value. webrtc_url points at the MediaMTX
    WebRTC player page under the fleet domain: <base>/<edge_id>/<ROOM>/. hls_url
    is reserved (left empty for now)."""
    base = mediamtx_client.stream_base(request.headers.get("host") or "localhost")
    camera.webrtc_url = mediamtx_client.webrtc_url(camera.camera_id, base)
    camera.hls_url = camera.hls_url or ""


@router.post("")
def create_camera(camera: Camera, request: Request):
    if camera_config.get_by_id(camera.camera_id):
        raise HTTPException(409, f"Camera exists: {camera.camera_id}")
    _normalize_urls(camera)
    _enrich_device_info(camera)
    _set_links(request, camera)
    camera_config.add(camera)
    _mtx_sync(request, camera)
    return {"status": "created", "camera_id": camera.camera_id}


@router.get("/status")
def all_camera_status(request: Request):
    return {
        cid: asdict(s)
        for cid, s in _mgr(request).get_status().items()
    }


# =====================================================================
# CLOUD PTZ — label-based control from the ceravishealth backend.
# Flow: frontend -> backend REST -> frp tunnel -> THIS endpoint -> ONVIF camera.
# Two actions, and no third: a move STOPS ITSELF (api/ptz_control arms the
# auto-stop), and `revert` puts the camera back where it was framing before the
# override. Zoom is not accepted — it's client-side digital zoom in the player.
# =====================================================================

_PTZ_ACTIONS = ("move", "revert")


def _camera_by_label(label: str) -> Camera | None:
    """Resolve a CameraName label (KITCHEN, LIVING_ROOM, …) to a camera by its
    name, room, or id — the shared resolver in CameraConfig (recording playback
    resolves through the same one)."""
    return camera_config.get_by_label(label)


def _ptz_camera(label: str) -> Camera:
    """The camera this request is for, or the right 4xx explaining why not."""
    cam = _camera_by_label(label)
    if cam is None:
        ptz_control.log(False, label, "rejected: no camera for label", 404)
        raise HTTPException(404, f"no camera for label '{label}'")
    if not ptz_control.has_ptz(cam):
        ptz_control.log(False, label, "rejected: camera has no PTZ", 400)
        raise HTTPException(400, f"camera '{label}' has no PTZ")
    return cam


def _velocity(body: dict, key: str) -> float:
    """A pan/tilt velocity, clamped to the ONVIF -1..1 range a camera accepts.
    A non-numeric value is the caller's bug, so say so instead of 500-ing."""
    try:
        return max(-1.0, min(1.0, float(field(body, key, default=0) or 0)))
    except (TypeError, ValueError):
        raise HTTPException(400, f"'{key}' must be a number between -1 and 1")


def _duration_ms(body: dict) -> int:
    """How long to move. 0/absent = the ptz_max_move_ms ceiling, which is also
    the clamp — ptz_control owns both."""
    try:
        return int(float(field(body, "durationMs", "duration_ms", default=0) or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "'durationMs' must be a number of milliseconds")


@router.post("/ptz")
def ptz_by_label(body: dict):
    """
    Pan/tilt ONE camera by its CameraName label — the cloud control call.

        { "edgeId":"<edge_id>", "cameraLabel":"KITCHEN",
          "action":"move", "pan":-0.4, "tilt":0, "durationMs":300 }
        { "edgeId":"<edge_id>", "cameraLabel":"KITCHEN", "action":"revert" }

    action (camelCase or snake_case everywhere):
      move   (default) — start pan/tilt at these velocities (-1..1). The EDGE
                         stops it after durationMs, clamped to ptz_max_move_ms,
                         so there is no stop to send and none to lose.
      revert           — drive back to the framing the camera held before the
                         first move of this override, and forget it. Idempotent:
                         a camera already home answers "unchanged".

    zoom is not accepted (client-side digital zoom). Auth = the edgeId match
    (api/control_auth): the request must carry THIS device's edge_id.
    """
    body = body or {}
    req_id = str(field(body, "edgeId", "edge_id", default="")).strip()
    label = str(field(body, "cameraLabel", "camera_label", "cameraNumber",
                      default=""))
    try:
        check_edge_id(req_id)
    except HTTPException:
        ptz_control.log(False, label, "rejected: edgeId auth (this device is "
                                      f"'{effective_edge_id()}')", 409)
        raise

    action = str(field(body, "action", default="move") or "move").strip().lower()
    if action not in _PTZ_ACTIONS:
        detail = "action must be 'move' or 'revert'"
        if action == "stop":                  # the removed action, named explicitly
            detail += " — a move stops itself, so there is no stop to send"
        ptz_control.log(False, label, f"rejected: {detail}", 400)
        raise HTTPException(400, detail)

    cam = _ptz_camera(label)
    from onvif.soap import OnvifError

    if action == "revert":
        try:
            reverted = ptz_control.revert(cam)
        except OnvifError as exc:
            ptz_control.log(False, label, f"revert failed: {exc}", 502)
            raise HTTPException(502, f"PTZ revert failed: {exc}")
        ptz_control.log(True, label,
                        "revert" if reverted else "revert: already at its "
                                                  "original framing", 200)
        return {"status": "reverted" if reverted else "unchanged",
                "reverted": reverted, "camera_id": cam.camera_id,
                "label": canon(label)}

    try:
        pan, tilt = _velocity(body, "pan"), _velocity(body, "tilt")
        if not (pan or tilt):
            raise HTTPException(400, "a move needs a non-zero pan or tilt")
        duration_ms = _duration_ms(body)
    except HTTPException as exc:              # a malformed body is worth seeing
        ptz_control.log(False, label, f"rejected: {exc.detail}", 400)
        raise
    try:
        used_ms = ptz_control.move(cam, pan, tilt, duration_ms=duration_ms)
    except OnvifError as exc:
        ptz_control.log(False, label, f"camera error: {exc}", 502)
        raise HTTPException(502, f"PTZ failed: {exc}")
    ptz_control.log(True, label,
                    f"move pan={pan:+.2f} tilt={tilt:+.2f} {used_ms}ms", 200)
    return {"status": "moving", "camera_id": cam.camera_id, "label": canon(label),
            "pan": pan, "tilt": tilt, "auto_stop_ms": used_ms}


# =====================================================================
# PATH-PARAM ROUTES AFTER
# =====================================================================

@router.get("/{camera_id}")
def get_camera(camera_id: str):
    camera = camera_config.get_by_id(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    return camera


@router.put("/{camera_id}")
def update_camera(camera_id: str, camera: Camera, request: Request):
    _normalize_urls(camera)
    _enrich_device_info(camera)
    _set_links(request, camera)
    if not camera_config.update(camera_id, camera):
        raise HTTPException(404, "Camera not found")
    _mtx_sync(request, camera)
    return {"status": "updated", "camera_id": camera_id}


@router.delete("/{camera_id}")
def delete_camera(camera_id: str, request: Request):
    if not camera_config.delete(camera_id):
        raise HTTPException(404, "Camera not found")
    _mtx_remove(request, camera_id)
    ptz_control.forget_home(camera_id)        # no stale framing to drive back to
    return {"status": "deleted", "camera_id": camera_id}


@router.get("/{camera_id}/status")
def camera_status(camera_id: str, request: Request):
    status = _mgr(request).get_camera_status(camera_id)
    if status is None:
        raise HTTPException(404, "Camera not found")
    return asdict(status)


def _mtx_safe(fn, backbone: bool, what: str) -> None:
    """Run a MediaMTX path op best-effort: skipped when the backbone is down (a
    dev box with no MediaMTX), and a MediaMTX error is logged but never fails the
    camera action — the AI-reader step still runs, so control degrades cleanly."""
    if not backbone:
        return
    try:
        fn()
    except MediaMTXError as exc:
        logger.warning("camera-control: MediaMTX %s failed: %s", what, exc)


def _apply_camera_action(request: Request, cam, action: str) -> bool:
    """Apply start/stop/restart to the camera's LIVE feed — the MediaMTX main
    stream (what the browser live view AND the AI reader consume) plus the AI
    reader — so a viewer's control acts on the real camera, not the AI only.
    Only the LIVE main path is touched: the independent, person-triggered
    RECORDING (RecordingController + MediaMTX's own path resilience) keeps running
    untouched. MediaMTX steps are best-effort; the returned bool is the reader.

      start   : bring up the live path, then start the reader
      stop    : stop the reader, then drop the live path (the live view stops)
      restart : force MediaMTX to RE-PULL the camera (recovers a dead live feed),
                then bounce the reader onto the fresh stream
    """
    backbone = mediamtx_client.is_up()
    mgr = _mgr(request)
    if action == "start":
        _mtx_safe(lambda: mediamtx_client.sync_live_path(cam), backbone,
                  f"sync {cam.camera_id}")
        return mgr.start_camera(cam.camera_id)
    if action == "stop":
        ok = mgr.stop_camera(cam.camera_id)
        _mtx_safe(lambda: mediamtx_client.remove_live_path(cam.camera_id), backbone,
                  f"remove {cam.camera_id}")
        return ok

    def _repull() -> None:                      # restart: reconnect from scratch
        mediamtx_client.remove_live_path(cam.camera_id)
        mediamtx_client.sync_live_path(cam)
    _mtx_safe(_repull, backbone, f"re-pull {cam.camera_id}")
    return mgr.restart_camera(cam.camera_id)


# action -> past-tense status word for the response.
_CAMERA_ACTIONS = {"start": "started", "stop": "stopped", "restart": "restarted"}


@router.post("/control")
def camera_control(body: dict, request: Request):
    """
    Start / stop / restart one camera's ingestion by its CameraName label — the
    cloud-driven twin of PTZ and recording playback (the three old per-id
    endpoints folded into one). Body (camelCase or snake_case):
        { "edgeId": "<edge_id>", "cameraLabel": "KITCHEN", "action": "restart" }
    action ∈ start | stop | restart. Auth = the edgeId match (api/control_auth):
    the request must carry THIS device's edge_id — the SAME one rule PTZ and
    playback use. Every hit (and rejection) lands on the sync console.
    """
    body = body or {}
    req_id = str(field(body, "edgeId", "edge_id", default="")).strip()
    action = str(field(body, "action", default="")).strip().lower()
    label = str(field(body, "cameraLabel", "camera_label", "cameraNumber",
                      default=""))
    try:
        check_edge_id(req_id)
    except HTTPException:
        call_log.record("camera-control", False, status=409,
                        label=f"{canon(label)} {action or '?'} rejected: edgeId "
                              f"auth (this device is '{effective_edge_id()}')")
        raise
    if action not in _CAMERA_ACTIONS:
        raise HTTPException(400, "action must be one of: start, stop, restart")
    cam = _camera_by_label(label)
    if cam is None:
        call_log.record("camera-control", False, status=404,
                        label=f"{canon(label)} {action}: no such camera")
        raise HTTPException(404, f"no camera for label {label!r}")
    ok = _apply_camera_action(request, cam, action)
    call_log.record("camera-control", bool(ok), status=200 if ok else 409,
                    label=f"{canon(label)} {action}")
    if not ok:
        raise HTTPException(409, f"camera {action} failed for {cam.camera_id}")
    return {"status": _CAMERA_ACTIONS[action], "action": action,
            "camera_id": cam.camera_id, "cameraLabel": canon(label)}


@router.get("/{camera_id}/frame")
def latest_frame_metadata(camera_id: str, request: Request):
    frame = _mgr(request).get_frame(camera_id)
    if frame is None:
        raise HTTPException(404, "No frame available")
    return {
        "camera_id": frame.camera_id,
        "frame_id": frame.frame_id,
        "timestamp": frame.timestamp,
        "width": frame.width,
        "height": frame.height,
        "fps": frame.fps,
    }


@router.get("/{camera_id}/snapshot")
def latest_snapshot(camera_id: str, request: Request, quality: int = 80):
    """JPEG snapshot of the most recent frame — used by the zone labeler."""
    frame = _mgr(request).get_frame(camera_id)
    if frame is None:
        raise HTTPException(404, "No frame available")
    ok, buf = cv2.imencode(
        ".jpg", frame.frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)],
    )
    if not ok:
        raise HTTPException(500, "Encode failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@router.get("/{camera_id}/time")
def camera_time(camera_id: str):
    """Compare the camera's OWN clock (ONVIF GetSystemDateAndTime) with the edge
    clock. The camera burns its clock into the video as the OSD timestamp, while
    a snapshot's reported time comes from the EDGE clock — so a large skew means
    the two won't line up. This is the diagnostic to confirm they do (both should
    track the same NTP source). Read-only, unauthenticated ONVIF call."""
    cam = camera_config.get_by_id(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if not cam.onvif_xaddr:
        raise HTTPException(400, "Camera has no ONVIF endpoint (added manually) — "
                                 "no camera clock to read")
    from onvif.client import OnvifCamera
    from onvif.soap import OnvifError
    edge = clock.now()
    try:
        cam_utc = OnvifCamera(cam.onvif_xaddr, cam.onvif_username or "",
                              cam.onvif_password or "").system_datetime()
    except OnvifError as exc:
        raise HTTPException(502, f"could not read camera clock: {exc}")
    skew = (edge - cam_utc).total_seconds()       # aware subtraction, tz-correct
    return {
        "cameraId": camera_id,
        "edgeTime": edge.isoformat(),
        "cameraTime": cam_utc.astimezone(clock.local_tz()).isoformat(),
        "skewSeconds": round(skew, 3),
        "skewMs": round(skew * 1000),
        "inSync": abs(skew) <= 2.0,
        "note": ("edge is ahead of the camera" if skew > 0 else
                 "camera is ahead of the edge" if skew < 0 else "aligned")
                + " — the OSD burned into frames follows the CAMERA clock; keep "
                  "both on the same NTP server so snapshot timestamps match it",
    }


@router.post("/{camera_id}/ptz")
def ptz(camera_id: str, body: dict):
    """
    The INSTALLER pad (ui/cameras.html): pan/tilt/zoom one camera by its raw
    camera_id, move on button-press and stop on release.

    Two deliberate differences from the cloud endpoint above. It keeps a stop
    (the browser holds the button, so a held move must not time out), and it
    honours optical `zoom` — this is the tool used while aiming a camera during
    setup, on the LAN, behind the admin login. It shares the same core, so a
    later cloud `revert` also undoes an installer's nudge.
    Body: { "pan": -1..1, "tilt": -1..1, "zoom": -1..1 } or { "action": "stop" }.
    """
    cam = camera_config.get_by_id(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if not ptz_control.has_ptz(cam):
        raise HTTPException(400, "Camera has no PTZ (or was added manually — "
                                 "re-add it via discovery to enable PTZ)")
    from onvif.soap import OnvifError
    body = body or {}
    pan = float(body.get("pan", 0) or 0)
    tilt = float(body.get("tilt", 0) or 0)
    zoom = float(body.get("zoom", 0) or 0)
    halt = body.get("action") == "stop" or not (pan or tilt or zoom)
    try:
        if halt:
            ptz_control.stop(cam)
        else:                                 # duration None: the browser stops it
            ptz_control.move(cam, pan, tilt, zoom, duration_ms=None)
    except OnvifError as exc:
        raise HTTPException(502, f"PTZ failed: {exc}")
    return {"status": "stopped" if halt else "moving", "camera_id": camera_id}


@router.post("/probe")
def probe_rtsp(body: dict):
    """
    Lightweight check: try to open an RTSP URL and grab one frame.
    Used by the 'Add camera' UI to confirm a URL works before saving.

    Forces interleaved TCP (like the AI ingest does): a 4K / H.265 main stream
    loses large fragmented packets over the default UDP transport and never
    yields a frame, while a smaller H.264 sub-stream would — the classic "the
    sub works but the main doesn't". The URL is credential-normalized first so a
    password containing '@' (encoded as %40) is handled correctly. Returns the
    normalized `url` so the UI can adopt the exact working string.
    """
    url = normalize_rtsp_url((body or {}).get("rtsp_url", ""))
    if not url:
        raise HTTPException(400, "rtsp_url required")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        return {"ok": False, "url": url,
                "reason": "could not open stream — check the URL, credentials, "
                          "and that the camera is reachable"}
    # H.265 must reach a keyframe before the first frame decodes; give it a
    # short window rather than failing on the first empty read.
    frame = None
    # monotonic: a wall-clock deadline breaks if NTP steps the clock
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
            break
        time.sleep(0.05)
    cap.release()
    if frame is None:
        return {"ok": False, "url": url,
                "reason": "opened but no frame — a 4K/H.265 stream on a weak "
                          "link, or the wrong stream path (try the sub-stream)"}
    h, w = frame.shape[:2]
    return {"ok": True, "url": url, "width": int(w), "height": int(h)}
