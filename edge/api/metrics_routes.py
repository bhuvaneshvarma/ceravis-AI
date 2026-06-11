from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/v1/metrics", tags=["Metrics"])


def _postures(state) -> dict:
    out: dict = {}
    if not hasattr(state, "posture_buffer"):
        return out
    for cam, per_track in state.posture_buffer.get_all().items():
        out[cam] = {
            str(tid): {
                "posture": r.posture.value,
                "confidence": round(r.confidence, 2),
                "torso_angle_deg": round(r.torso_angle_deg, 1),
                "knee_angle_deg": round(r.knee_angle_deg, 1),
            }
            for tid, r in per_track.items()
        }
    return out


@router.get("")
def all_metrics(request: Request) -> dict:
    state = request.app.state
    camera_status = {
        cid: asdict(s) for cid, s in state.camera_manager.get_status().items()
    }
    detection = {
        "running": state.detection_runner.is_running,
        "frames_processed": state.detection_runner.frames_processed,
        "detections_generated": state.detection_runner.detections_generated,
    }
    return {
        "cameras": camera_status,
        "detection": detection,
        "pipelines": state.metrics_registry.snapshot_all(),
        "system": state.system_monitor.snapshot(),
        "postures": _postures(state),
    }


@router.get("/system")
def system_metrics(request: Request) -> dict:
    return request.app.state.system_monitor.snapshot()


@router.get("/pipelines")
def pipeline_metrics(request: Request) -> list[dict]:
    return request.app.state.metrics_registry.snapshot_all()


@router.get("/postures")
def postures(request: Request) -> dict:
    return _postures(request.app.state)
