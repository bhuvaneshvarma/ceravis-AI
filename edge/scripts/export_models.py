#!/usr/bin/env python3
"""
Build TensorRT engines using JetPack's own tooling (trtexec).

Two stages, both idempotent:

  1. ONNX export — needs ultralytics + CPU torch. Run once via
     scripts/export_engines.sh, which creates a disposable venv for it.
     Inference NEVER uses torch; it runs pure TensorRT (see detection/).

  2. ONNX -> FP16 .engine via trtexec — ships with JetPack at
     /usr/src/tensorrt/bin/trtexec, no Python deps at all.

If an engine already exists it is left alone. If the ONNX exists but the
engine is missing (e.g. after re-flashing), only stage 2 runs.

Engines are GPU-arch-specific — always built on the device.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlretrieve


def _env(key: str, default: str) -> str:
    v = os.environ.get(key, "").strip()
    return v or default


def _have(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _trtexec() -> str | None:
    found = shutil.which("trtexec")
    if found:
        return found
    jetpack_path = Path("/usr/src/tensorrt/bin/trtexec")
    return str(jetpack_path) if jetpack_path.exists() else None


# ---------------------------------------------------------------- stage 1
def export_onnx(weights: str, onnx_path: Path, imgsz: int) -> bool:
    """Export YOLO weights to ONNX on CPU. Returns True if ONNX is present."""
    if _have(onnx_path):
        return True
    try:
        from ultralytics import YOLO
    except ImportError:
        print(f"[skip] {onnx_path.name}: ultralytics not installed — "
              "run scripts/export_engines.sh once to create the ONNX")
        return False

    print(f"[onnx] {weights} -> {onnx_path}  (imgsz={imgsz}, cpu)")
    # YOLO26 exports its end-to-end (NMS-free) head by default — output is
    # final detections [x1,y1,x2,y2,conf,cls], which is exactly what
    # yolo_detector.py / yolo_pose.py parse. Let ultralytics pick the opset.
    out = YOLO(weights).export(format="onnx", imgsz=imgsz, device="cpu")
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    Path(out).replace(onnx_path)
    return True


# ---------------------------------------------------------------- stage 2
def build_engine(onnx_path: Path, engine_path: Path,
                 extra_args: list[str] | None = None) -> None:
    """ONNX -> FP16 TensorRT engine via JetPack's trtexec."""
    if _have(engine_path):
        print(f"[skip] {engine_path.name} already built")
        return
    if not _have(onnx_path):
        print(f"[skip] {engine_path.name}: missing {onnx_path}")
        return
    trtexec = _trtexec()
    if trtexec is None:
        print("[error] trtexec not found — is the JetPack TensorRT "
              "component installed? (expected /usr/src/tensorrt/bin/trtexec)")
        return

    engine_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        trtexec,
        f"--onnx={onnx_path}",
        "--fp16",
        f"--saveEngine={engine_path}",
        "--memPoolSize=workspace:2048",
    ] + (extra_args or [])
    print(f"[trtexec] {onnx_path.name} -> {engine_path.name}  (FP16, ~3-8 min)")
    rc = subprocess.run(cmd).returncode
    if rc == 0 and _have(engine_path):
        print(f"[ok] {engine_path}")
    else:
        print(f"[error] trtexec failed (rc={rc}) for {engine_path.name}")


# --------------------------------------------------------------- FastReid
def export_fastreid(engine_path: Path) -> None:
    """
    FastReid: download a pre-exported ONNX (FASTREID_ONNX_URL) then build.
    Produce the ONNX once on a workstation with:

        python tools/deploy/onnx_export.py \
            --config-file configs/Market1501/bagtricks_R50.yml \
            --output bot_r50.onnx --opset 13
    """
    if _have(engine_path):
        print(f"[skip] {engine_path.name} already built")
        return

    onnx_path = engine_path.with_suffix(".onnx")
    if not _have(onnx_path):
        onnx_url = _env("FASTREID_ONNX_URL", "")
        if not onnx_url:
            print("[skip] FASTREID_ONNX_URL not set — ReID disabled "
                  "(set it in jetson.env to enable)")
            return
        print(f"[download] {onnx_url}")
        onnx_path.parent.mkdir(parents=True, exist_ok=True)
        urlretrieve(onnx_url, onnx_path)

    build_engine(onnx_path, engine_path, extra_args=[
        "--minShapes=input:1x3x256x128",
        "--optShapes=input:1x3x256x128",
        "--maxShapes=input:8x3x256x128",
    ])


# ---------------------------------------------------------------- main
def main() -> int:
    # --onnx-only: run just stage 1 (needs torch/ultralytics). The engine
    # builds then happen in a separate, torch-free process — keeping ~2 GB
    # of RAM free for trtexec, which otherwise can fail CUDA init on the
    # shared-memory Orin Nano ("no CUDA-capable device is detected").
    onnx_only = "--onnx-only" in sys.argv

    det_w = _env("DETECTION_WEIGHTS", "yolo26m.pt")
    pose_w = _env("POSE_WEIGHTS", "yolo26m-pose.pt")
    det_engine = Path(_env("DETECTION_MODEL_PATH", "models/detection/yolo26m.engine"))
    pose_engine = Path(_env("POSE_MODEL_PATH", "models/pose/yolo26m-pose.engine"))
    reid_engine = Path(_env("REID_MODEL_PATH", "models/reid/fastreid_bot_r50.engine"))

    det_ready = export_onnx(det_w, det_engine.with_suffix(".onnx"),
                            imgsz=int(_env("DETECTION_INPUT_SIZE", "640")))
    pose_ready = export_onnx(pose_w, pose_engine.with_suffix(".onnx"),
                             imgsz=int(_env("POSE_INPUT_SIZE", "640")))
    if onnx_only:
        print("[done] ONNX pass complete — engines build in the next pass")
        return 0

    if det_ready:
        build_engine(det_engine.with_suffix(".onnx"), det_engine)
    if pose_ready:
        build_engine(pose_engine.with_suffix(".onnx"), pose_engine)

    export_fastreid(reid_engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
