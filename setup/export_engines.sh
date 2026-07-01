#!/usr/bin/env bash
# =============================================================
# One-time model export — CERAVIS, Jetson native
# =============================================================
# torch is ONLY needed to export YOLO weights to ONNX. Inference runs pure
# TensorRT. So we use a DISPOSABLE venv with small CPU-only torch wheels
# (no CUDA torch, no NVIDIA wheel hunting), export the ONNX, then build
# FP16 engines with JetPack's own trtexec.
#
# Run once:  bash setup/export_engines.sh
# Re-run anytime — every stage skips work that is already done.
# Afterwards you can reclaim ~1.5 GB with:  rm -rf /tmp/ceravis-export-venv
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
EDGE_DIR="$REPO_DIR/edge"
VENV="${VENV:-/tmp/ceravis-export-venv}"

cd "$EDGE_DIR"

if [ ! -x "$VENV/bin/python" ]; then
    echo "== creating export venv at $VENV =="
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip
    echo "== CPU-only torch (small; inference never uses torch) =="
    "$VENV/bin/pip" install torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu
    "$VENV/bin/pip" install ultralytics onnx onnxslim
fi

# Load model paths/weights config so the script and the app agree.
set -a
# shellcheck disable=SC1091
. "$EDGE_DIR/infra/env/jetson.env"
set +a

echo "== pass 1/2: ONNX export (venv, CPU torch) =="
"$VENV/bin/python" "$SETUP_DIR/export_models.py" --onnx-only

echo "== pass 2/2: TensorRT engine build (fresh torch-free process) =="
# Separate process so torch's ~2 GB is released before trtexec runs —
# on the shared-memory Orin Nano that headroom is what lets CUDA init.
python3 "$SETUP_DIR/export_models.py"

echo
echo "Engines in $EDGE_DIR/models/. Optional cleanup: rm -rf $VENV"
