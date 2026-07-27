from __future__ import annotations

import logging
import time

import cv2
import requests
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from configuration.account_config import account_recipient
from enrollment.enrollment_manager import EnrollmentManager
from integration.ceravis_api import CeravisApiError, get_patient_postures


router = APIRouter(prefix="/api/v1/recipients", tags=["Recipients"])

enrollment_manager = EnrollmentManager()
logger = logging.getLogger("enrollment")

# getPatientPostures view keys -> the <view> half of the enrollment label.
_POSTURE_VIEWS = (("frontView", "front"), ("backView", "back"),
                  ("leftView", "left"), ("rightView", "right"))


def _worker(request: Request):
    return getattr(request.app.state, "enroll_worker", None)


def _media_payload(recipient_id: str) -> dict:
    names = enrollment_manager.media_names(recipient_id)
    labels = enrollment_manager.get_labels(recipient_id)
    return {
        "count": len(names),
        "frames": [
            {"name": n, "label": labels.get(n, ""),
             "url": f"/api/v1/recipients/{recipient_id}/media/{n}"}
            for n in names
        ],
    }


# ---- the one recipient (= the verified account holder) ---------------
# There is no recipients.json: the care recipient IS the account (see
# configuration.account_config.account_recipient). These endpoints keep the
# same shape the wizard already consumes — a single-element list.
@router.get("")
def list_recipients():
    r = account_recipient()
    if r is None:
        return []
    r = dict(r)
    r["enrollment"] = enrollment_manager.get_status(r["recipient_id"])
    return [r]


@router.post("")
def create_recipient():
    """Bind the verified account holder as this device's care recipient and
    prepare their enrollment folder (idempotent). Identity comes from
    account.json — no body needed, no separate recipient record is written."""
    r = account_recipient()
    if r is None:
        raise HTTPException(400, "verify the account first — no patient to enroll")
    rid = r["recipient_id"]
    enrollment_manager.create_recipient_folder(rid)
    if enrollment_manager.get_status(rid).get("state", "none") == "none":
        enrollment_manager.set_status(rid, state="none",
                                      message="registered — add enrollment media")
    return {"status": "ready", "recipient_id": rid, "full_name": r["full_name"]}


@router.get("/{recipient_id}/enroll/status")
def enroll_status(recipient_id: str):
    return enrollment_manager.get_status(recipient_id)


@router.get("/{recipient_id}/embeddings/stats")
def embedding_stats(recipient_id: str):
    """Enrolled vs live-captured (adaptive) embedding counts, last capture
    time and label breakdown — so you can confirm live capture is working."""
    return enrollment_manager.embedding_stats(recipient_id)


@router.get("/{recipient_id}/media")
def list_media(recipient_id: str):
    return _media_payload(recipient_id)


@router.get("/{recipient_id}/media/{name}")
def get_media(recipient_id: str, name: str):
    path = enrollment_manager.media_path(recipient_id, name)
    if path is None:
        raise HTTPException(404, "not found")
    return FileResponse(str(path), media_type="image/jpeg")


# ---- cloud posture images (getPatientPostures) -----------------------
@router.post("/{recipient_id}/postures/import")
def import_postures(recipient_id: str):
    """Pull the patient's posture images from the CERAVIS app
    (PUT /v1/ai/getPatientPostures) into this recipient's enrollment folder,
    each labeled '<view>/<posture>' (e.g. 'front/standing'), so the SAME
    enrollment worker embeds them alongside any on-device captures — one vector
    per image, added to the gallery.

    Called when the wizard reaches Enrollment (after the zoning step). Identity
    and the patientUserId come from the verified account, so the path
    recipient_id is only used for the on-disk folder. Idempotent: images already
    imported (keyed by postureImageId) are skipped. Best-effort — a patient with
    no cloud postures just falls back to on-device capture."""
    rec = account_recipient()
    if rec is None:
        raise HTTPException(400, "verify the account first — no patient to enroll")
    pid, rid = rec["patient_user_id"], rec["recipient_id"]
    try:
        postures = get_patient_postures(pid)
    except CeravisApiError as exc:
        raise HTTPException(502, f"getPatientPostures failed: {exc}")

    imported = skipped = failed = 0
    preview: list[dict] = []
    for entry in postures:
        posture = str(entry.get("postureType") or "").strip().lower() or "unknown"
        for key, view in _POSTURE_VIEWS:
            v = entry.get(key)
            if not isinstance(v, dict) or not v.get("downloadUrl"):
                continue
            img_id = v.get("postureImageId") or f"{posture}_{view}"
            name, label = f"cloud_{img_id}.jpg", f"{view}/{posture}"
            preview.append({"name": name, "label": label,
                            "url": f"/api/v1/recipients/{rid}/media/{name}",
                            "embeddingStatus": v.get("embeddingStatus")})
            enrollment_manager.record_label(rid, name, label)
            if enrollment_manager.has_photo(rid, name):
                skipped += 1
                continue
            try:
                r = requests.get(v["downloadUrl"], timeout=20)
                r.raise_for_status()
                enrollment_manager.import_photo(rid, name, r.content)
                imported += 1
            except Exception:
                logger.warning("posture import failed for %s (%s)", name, label)
                failed += 1

    if imported:
        enrollment_manager.set_status(
            rid, state="review",
            message=f"{imported} posture image(s) imported from the app — "
                    f"review then Enroll")
    logger.info("postures import: user=%s imported=%d skipped=%d failed=%d",
                pid, imported, skipped, failed)
    return {"recipient_id": rid, "patient_user_id": pid, "count": len(preview),
            "imported": imported, "skipped": skipped, "failed": failed,
            "postures": preview}


# ---- capture / upload media (stored only — NOT enrolled yet) ---------
@router.post("/{recipient_id}/enroll/photos")
async def enroll_photos(recipient_id: str, files: list[UploadFile] = File(...)):
    saved = 0
    for f in files:
        data = await f.read()
        if not data:
            continue
        ext = (f.filename or "img.jpg").rsplit(".", 1)[-1]
        enrollment_manager.save_photo(recipient_id, data, ext)
        saved += 1
    if saved == 0:
        raise HTTPException(400, "no images received")
    enrollment_manager.set_status(recipient_id, state="review",
                                  message=f"{saved} photo(s) added — review then Enroll")
    return _media_payload(recipient_id)


@router.post("/{recipient_id}/enroll/video")
async def enroll_video(recipient_id: str, file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty video")
    ext = (file.filename or "v.mp4").rsplit(".", 1)[-1]
    enrollment_manager.save_video(recipient_id, data, ext)
    enrollment_manager.set_status(recipient_id, state="review",
                                  message="video added — review then Enroll")
    return {"status": "stored", "video": True}


@router.post("/{recipient_id}/enroll/capture")
def enroll_capture(recipient_id: str, request: Request, body: dict):
    """
    Save ONE live frame as an enrollment photo, with an optional label
    (e.g. 'front/standing'). Drives the new live-capture UI: the user lines up
    a pose/viewpoint, picks the label, and clicks the red capture button.
    """
    camera_id = (body or {}).get("camera_id", "").strip()
    label = (body or {}).get("label", "").strip()
    if not camera_id:
        raise HTTPException(400, "camera_id required")
    fd = request.app.state.camera_manager.get_frame(camera_id)
    if fd is None:
        raise HTTPException(404, "no frame available — is the camera live?")
    ok, buf = cv2.imencode(".jpg", fd.frame)
    if not ok:
        raise HTTPException(500, "encode failed")
    path = enrollment_manager.save_photo(recipient_id, buf.tobytes(), "jpg")
    enrollment_manager.record_label(recipient_id, path.name, label)
    count = len(enrollment_manager.media_names(recipient_id))
    enrollment_manager.set_status(
        recipient_id, state="review",
        message=f"{count} frame(s) captured — review then Enroll")
    return _media_payload(recipient_id)


@router.post("/{recipient_id}/enroll/live")
def enroll_live(recipient_id: str, request: Request, body: dict):
    camera_id = (body or {}).get("camera_id", "").strip()
    seconds = float((body or {}).get("seconds", 4))
    per_sec = 3
    if not camera_id:
        raise HTTPException(400, "camera_id required")
    mgr = request.app.state.camera_manager
    captured, deadline, last_fid = 0, time.time() + seconds, -1
    while time.time() < deadline:
        fd = mgr.get_frame(camera_id)
        if fd is not None and fd.frame_id != last_fid:
            last_fid = fd.frame_id
            ok, buf = cv2.imencode(".jpg", fd.frame)
            if ok:
                enrollment_manager.save_photo(recipient_id, buf.tobytes(), "jpg")
                captured += 1
        time.sleep(1.0 / per_sec)
    if captured == 0:
        raise HTTPException(404, "no frames captured — is the camera live?")
    enrollment_manager.set_status(recipient_id, state="review",
                                  message=f"{captured} live frame(s) — review then Enroll")
    return _media_payload(recipient_id)


# ---- commit: generate embeddings from the reviewed media -------------
@router.post("/{recipient_id}/enroll/commit")
def enroll_commit(recipient_id: str, request: Request):
    if not enrollment_manager.media_names(recipient_id):
        raise HTTPException(400, "no media to enroll — capture/upload first")
    w = _worker(request)
    if w is None:
        enrollment_manager.set_status(recipient_id, state="pending_reid",
                                      message="worker offline")
        raise HTTPException(503, "enrollment worker offline")
    w.enqueue(recipient_id)
    return {"status": "queued"}
