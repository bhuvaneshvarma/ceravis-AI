#!/usr/bin/env python3
"""
Build TensorRT engines on the Jetson (called once at container start).

Engines are GPU-arch-specific — must be built on the device. Idempotent.

Models:
  - Detection : YOLO26m  (configurable via DETECTION_WEIGHTS env var,
                          defaults to yolov8m.pt; ultralytics autodownloads)
  - Pose      : YOLO26m-Pose (POSE_WEIGHTS, defaults to yolov8m-pose.pt)
  - ReID      : FastReid BoT_R50 ONNX -> trtexec FP16
                (set FASTREID_ONNX_URL to enable; otherwise ReID stays off)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.request import urlretrieve


MODELS_ROOT = Path("/models")
DETECT_DIR = MODELS_ROOT / "detection"
POSE_DIR = MODELS_ROOT / "pose"
REID_DIR = MODELS_ROOT / "reid"

for d in (DETECT_DIR, POSE_DIR, REID_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- utils
def _have(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _env(key: str, default: str) -> str:
    v = os.environ.get(key, "").strip()
    return v or default


# ---------------------------------------------------------------- YOLO
def export_yolo(weight_name: str, engine_path: Path, imgsz: int = 640) -> None:
    if _have(engine_path):
        print(f"[skip] {engine_path.name} already present")
        return
    print(f"[export] {weight_name} -> {engine_path.name}  (FP16, imgsz={imgsz})")
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ultralytics missing — cannot export YOLO")
        return

    model = YOLO(weight_name)  # autodownload .pt
    out = model.export(format="engine", half=True, imgsz=imgsz, device=0)
    Path(out).rename(engine_path)
    print(f"[ok] {engine_path}")


# --------------------------------------------------------------- FastReid
def export_fastreid(engine_path: Path) -> None:
    """
    FastReid path: download an ONNX (already exported from FastReid repo)
    then convert with trtexec to FP16.

    You can produce the ONNX once on a workstation with:

        python tools/deploy/onnx_export.py \
            --config-file configs/Market1501/bagtricks_R50.yml \
            --output bot_r50.onnx --opset 13

    then host it somewhere and set FASTREID_ONNX_URL.
    """
    if _have(engine_path):
        print(f"[skip] {engine_path.name} already present")
        return

    onnx_url = _env("FASTREID_ONNX_URL", "")
    if not onnx_url:
        print("[skip] FASTREID_ONNX_URL not set — ReID disabled "
              "(set it in jetson.env to enable)")
        return

    onnx_path = engine_path.with_suffix(".onnx")
    if not _have(onnx_path):
        print(f"[download] {onnx_url}")
        urlretrieve(onnx_url, onnx_path)

    print(f"[trtexec] {onnx_path.name} -> {engine_path.name}  (FP16)")
    cmd = (
        f"trtexec --onnx={onnx_path} --fp16 "
        f"--saveEngine={engine_path} "
        f"--memPoolSize=workspace:1024 "
        f"--minShapes=input:1x3x256x128 "
        f"--optShapes=input:1x3x256x128 "
        f"--maxShapes=input:8x3x256x128"
    )
    rc = os.system(cmd)
    if rc != 0:
        print("[warn] trtexec failed — ReID will stay disabled")
    else:
        print(f"[ok] {engine_path}")


# ---------------------------------------------------------------- main
def main() -> int:
    det_w = _env("DETECTION_WEIGHTS", "yolov8m.pt")
    pose_w = _env("POSE_WEIGHTS", "yolov8m-pose.pt")
    det_engine = Path(_env("DETECTION_MODEL_PATH", "/models/detection/yolo26m.engine"))
    pose_engine = Path(_env("POSE_MODEL_PATH", "/models/pose/yolo26m-pose.engine"))
    reid_engine = Path(_env("REID_MODEL_PATH", "/models/reid/fastreid_bot_r50.engine"))

    export_yolo(det_w, det_engine, imgsz=int(_env("DETECTION_INPUT_SIZE", "640")))
    export_yolo(pose_w, pose_engine, imgsz=int(_env("POSE_INPUT_SIZE", "640")))
    export_fastreid(reid_engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
