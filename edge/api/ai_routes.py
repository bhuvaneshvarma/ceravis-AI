from __future__ import annotations

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/v1/ai", tags=["AI"])


@router.get("/state")
def ai_state(request: Request, camera_id: str | None = None):
    """
    Live AI state per camera, merged from the pipeline buffers:
    ByteTrack boxes + posture classification + ReID identity.

    The monitor UI polls this a few times per second and draws the
    boxes/labels over the live JPEG stream client-side — zero extra
    GPU cost on the device.
    """
    state = request.app.state
    tracks_by_cam = state.track_buffer.get_all()
    postures = state.posture_buffer.get_all()
    identities = state.identity_buffer.get_all()
    detections = state.detection_buffer.get_all()
    target_reg = getattr(state, "target_registry", None)

    # Union of cameras seen by detection and tracking, so the monitor can
    # tell "detector sees N people but tracker has 0" (a tracking problem)
    # apart from "detector sees 0" (a detection problem).
    cams = set(tracks_by_cam) | set(detections)
    out: dict = {}
    for cam in cams:
        if camera_id and cam != camera_id:
            continue
        result = tracks_by_cam.get(cam)
        det = detections.get(cam)
        cam_postures = postures.get(cam, {})
        cam_identities = identities.get(cam, {})
        entries = []
        for t in (result.tracks if result else []):
            posture = cam_postures.get(t.track_id)
            identity = cam_identities.get(t.track_id)
            entries.append({
                "track_id": t.track_id,
                "bbox": [t.bbox.x1, t.bbox.y1, t.bbox.x2, t.bbox.y2],
                "confidence": round(t.confidence, 3),
                "posture": posture.posture.value if posture else None,
                "posture_confidence":
                    round(posture.confidence, 2) if posture else None,
                "recipient_id": identity.recipient_id if identity else None,
                "is_target": identity.is_target if identity else False,
                "reid_score": round(identity.confidence, 3) if identity else None,
                "view_label": identity.view_label if identity else None,
            })
        out[cam] = {
            "frame_id": (result.frame_id if result
                         else (det.frame_id if det else 0)),
            "timestamp": (result.timestamp.isoformat() if result
                          else (det.timestamp.isoformat() if det else "")),
            "detections": len(det.detections) if det else 0,
            "target_track_id": target_reg.get(cam) if target_reg else None,
            "tracks": entries,
        }
    return out
