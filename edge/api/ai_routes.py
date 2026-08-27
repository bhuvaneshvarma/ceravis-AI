from __future__ import annotations


from fastapi import APIRouter, Request

from common.freshness import TRACK_FRESH_SECS, is_fresh
from config.settings import settings
from common import clock


router = APIRouter(prefix="/api/v1/ai", tags=["AI"])


@router.get("/state")
def ai_state(request: Request, camera_id: str | None = None):
    """
    Live AI state per camera, merged from the pipeline buffers:
    ByteTrack boxes + posture classification + ReID identity.

    The monitor UI polls this a few times per second and draws the target
    marker on a transparent canvas over the WebRTC <video> client-side — no
    frame ever passes through this process, so it costs no GPU and no CPU.
    """
    state = request.app.state
    tracks_by_cam = state.track_buffer.get_all()
    postures = state.posture_buffer.get_all()
    identities = state.identity_buffer.get_all()
    detections = state.detection_buffer.get_all()
    target_reg = getattr(state, "target_registry", None)
    # The AI's OWN decoded frame size — the coordinate space bboxes live in. The
    # monitor scales the overlay dot by THIS, not by the <video> element's width:
    # when the AI reads a different-resolution stream than the browser shows
    # (e.g. a camera whose main stream is HEVC), the two differ and a dot scaled
    # by the video width lands in the wrong place.
    cam_mgr = getattr(state, "camera_manager", None)
    frame_buf = getattr(cam_mgr, "frame_buffer", None) if cam_mgr else None

    # Union of cameras seen by detection and tracking, so the monitor can
    # tell "detector sees N people but tracker has 0" (a tracking problem)
    # apart from "detector sees 0" (a detection problem).
    cams = set(tracks_by_cam) | set(detections)
    now = clock.now()
    out: dict = {}
    for cam in cams:
        if camera_id and cam != camera_id:
            continue
        result = tracks_by_cam.get(cam)
        det = detections.get(cam)
        cam_postures = postures.get(cam, {})
        cam_identities = identities.get(cam, {})
        # Idle cameras keep their last TrackResult forever — a stale one
        # would paint ghost boxes and let the monitor "follow" a target
        # that already left this room. Same freshness rule as the rules.
        live = result is not None and is_fresh(result.timestamp, now,
                                               TRACK_FRESH_SECS)
        entries = []
        for t in (result.tracks if live else []):
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
                # Recency cosine behind the (re)acquire — lets the monitor show
                # WHY this track won the ID, not just that it did.
                "recency_score": (round(identity.recency_score, 3)
                                  if identity and identity.recency_score is not None
                                  else None),
            })
        fd = frame_buf.get(cam) if frame_buf else None
        out[cam] = {
            "frame_id": (result.frame_id if result
                         else (det.frame_id if det else 0)),
            "timestamp": (result.timestamp.isoformat() if result
                          else (det.timestamp.isoformat() if det else "")),
            "detections": len(det.detections) if det else 0,
            "target_track_id": target_reg.get(cam) if target_reg else None,
            # The coordinate space `bbox` is in — the monitor scales the dot by
            # this so it lands correctly even when the AI and the browser see
            # different stream resolutions. null falls back to the video width.
            "frame_w": fd.width if fd else None,
            "frame_h": fd.height if fd else None,
            "tracks": entries,
        }
    return out


@router.get("/stillness")
def ai_stillness(request: Request):
    """Live no_motion / no_transition diagnostics — the observability this
    signal never had, and without which the field bug was invisible.

    Everything needed to answer "why is no_motion not firing" in one call:
    which channel is judging, what it measured, what it measured AGAINST (the
    threshold self-calibrates per camera), and how far each of the two clocks
    has actually run.
    """
    engine = getattr(request.app.state, "rule_engine", None)
    rule = getattr(engine, "stillness", None) if engine else None
    if rule is None:
        return {"available": False,
                "reason": "rule engine not running (no posture tracker?)"}

    v = rule.last_verdict
    window = settings.stillness_window_secs
    motion_still = float(getattr(rule, "_motion_still", 0.0) or 0.0)
    posture_hold = 0.0
    since = getattr(rule, "_posture_since", None)
    if since is not None:
        posture_hold = max(0.0, (clock.now() - since).total_seconds())

    out = {
        "available": True,
        "window_secs": window,
        "no_motion": {
            "still_secs": round(motion_still, 1),
            "remaining_secs": round(max(0.0, window - motion_still), 1),
            "will_fire": motion_still >= window,
        },
        "no_transition": {
            "held_secs": round(posture_hold, 1),
            "remaining_secs": round(max(0.0, window - posture_hold), 1),
            "posture": rule._posture.value if rule._posture else None,
        },
    }
    if v is None:
        out["verdict"] = None
        out["hint"] = ("no recipient sighting yet — no_motion needs a LOCKED "
                       "ReID target, not just a detected person")
        return out

    out["verdict"] = {
        "moving": v.moving,
        "reason": v.reason,
        "pose_ready": v.pose_ready,
        "pixel_ready": v.pixel_ready,
        "moved_joints": v.moved_joints,
        "pixel_diff": round(v.pixel_diff, 5) if v.pixel_diff is not None else None,
        "pixel_thresh": round(v.pixel_thresh, 5) if v.pixel_thresh is not None else None,
        "pixel_headroom": (round(v.pixel_thresh - v.pixel_diff, 5)
                           if v.pixel_thresh is not None
                           and v.pixel_diff is not None else None),
    }
    if not v.pixel_ready:
        out["hint"] = ("pixel channel not judging — the motion clock is HELD, so "
                       "no_motion can never mature. Either no frame for this "
                       "camera, the ROI is under pixel_still_min_roi_px, or the "
                       "scene noise floor is still calibrating "
                       f"({settings.pixel_noise_min_samples} samples needed).")
    elif v.pixel_diff is not None and v.pixel_thresh is not None \
            and v.pixel_diff > v.pixel_thresh:
        out["hint"] = ("pixels read as MOVING. If you are sitting perfectly "
                       "still, this scene is noisier than the trigger allows — "
                       "raise pixel_move_ratio until pixel_headroom goes "
                       "positive while still.")
    return out
