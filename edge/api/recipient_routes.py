from __future__ import annotations

import time

import cv2
from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from configuration.recipient_config import RecipientConfig
from enrollment.enrollment_manager import EnrollmentManager
from schemas.recipient import Recipient


router = APIRouter(prefix="/api/v1/recipients", tags=["Recipients"])

recipient_config = RecipientConfig()
enrollment_manager = EnrollmentManager()


def _worker(request: Request):
    return getattr(request.app.state, "enroll_worker", None)


# ---- CRUD ------------------------------------------------------------
@router.get("")
def list_recipients():
    out = []
    for r in recipient_config.get_all():
        rid = r.get("recipient_id")
        r["enrollment"] = enrollment_manager.get_status(rid) if rid else {}
        out.append(r)
    return out


@router.post("")
def create_recipient(recipient: Recipient):
    enrollment_manager.create_recipient_folder(recipient.recipient_id)
    recipient_config.add(recipient)
    enrollment_manager.set_status(recipient.recipient_id, state="none",
                                  message="registered — add enrollment media")
    return {"status": "created", "recipient_id": recipient.recipient_id}


@router.get("/{recipient_id}/enroll/status")
def enroll_status(recipient_id: str):
    return enrollment_manager.get_status(recipient_id)


# ---- enrollment: Option A — photos ----------------------------------
@router.post("/{recipient_id}/enroll/photos")
async def enroll_photos(recipient_id: str, request: Request,
                        files: list[UploadFile] = File(...)):
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
    _queue(request, recipient_id, f"{saved} photo(s) uploaded")
    return {"status": "queued", "saved": saved}


# ---- enrollment: Option B — video -----------------------------------
@router.post("/{recipient_id}/enroll/video")
async def enroll_video(recipient_id: str, request: Request,
                       file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty video")
    ext = (file.filename or "v.mp4").rsplit(".", 1)[-1]
    enrollment_manager.save_video(recipient_id, data, ext)
    _queue(request, recipient_id, "video uploaded")
    return {"status": "queued"}


# ---- enrollment: Option C — live camera capture ---------------------
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
    _queue(request, recipient_id, f"{captured} live frame(s) captured")
    return {"status": "queued", "captured": captured}


# ---- helper ----------------------------------------------------------
def _queue(request: Request, recipient_id: str, msg: str) -> None:
    w = _worker(request)
    if w is None:
        enrollment_manager.set_status(recipient_id, state="pending_reid",
                                      message=msg + " (worker offline)")
        return
    w.enqueue(recipient_id)
