#!/usr/bin/env bash
# =============================================================
# The AI models — every one of them (setup/export_models.py has the list).
# =============================================================
# torch is ONLY needed to turn the downloaded weights into ONNX; inference runs
# pure TensorRT. So the export tools live in a DISPOSABLE venv (CPU-only torch,
# ultralytics, torchreid — never in the app's own environment), and the engines
# are then built with JetPack's own trtexec in a fresh, torch-free process.
#
#   bash setup/export_engines.sh              on the device: ONNX (when missing)
#                                             + the TensorRT engines
#   bash setup/export_engines.sh --onnx-only  on ANY machine — the PC builds the
#                                             ONNX for a release bundle this way
#                                             (setup/make_release.sh), so a device
#                                             needs no internet and no torch
#
# Re-run anytime — every stage skips work that is already done. The venv is
# kept for next time; reclaim its ~1.5 GB with:  rm -rf "$VENV"
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup/lib.sh
. "$SETUP_DIR/lib.sh"      # EDGE_DIR, CERAVIS_PYTHON, and DATA_DIR/MODELS_DIR for the app
export_layout
ONNX_ONLY=0
[ "${1:-}" = "--onnx-only" ] && ONNX_ONLY=1
VENV="${VENV:-/tmp/ceravis-export-venv}"
HOST_PYTHON="$(host_python)"
vpy() { if [ -x "$VENV/bin/python" ]; then "$VENV/bin/python" "$@"; else "$VENV/Scripts/python.exe" "$@"; fi; }

cd "$EDGE_DIR"

if [ ! -x "$VENV/bin/python" ] && [ ! -x "$VENV/Scripts/python.exe" ]; then
    echo "== creating export venv at $VENV =="
    # System packages visible: JetPack's own numpy/OpenCV, or a PC's torch.
    "$HOST_PYTHON" -m venv --system-site-packages "$VENV"
    vpy -m pip install --upgrade pip
fi
echo "== export tools (CPU-only torch; inference never uses torch) =="
vpy -c "import torch" 2>/dev/null \
    || vpy -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
# ultralytics for YOLO26; torchreid (+ what it imports) for the ReID model;
# pydantic-settings so export_models.py reads the app's own settings.
vpy -m pip install "ultralytics>=8.4,<9" onnx onnxslim pydantic-settings \
    "torchreid==0.2.5" gdown tensorboard matplotlib "scipy>=1.10" opencv-python-headless

# Model settings from the app's config, so the script and the app agree.
set -a
# shellcheck disable=SC1091
. "$EDGE_DIR/infra/env/jetson.env"
set +a

echo "== pass 1: ONNX (venv, CPU torch) =="
vpy "$SETUP_DIR/export_models.py" --onnx-only
if [ "$ONNX_ONLY" = 1 ]; then
    echo; echo "ONNX models in $(models_dir)/ (engines are built on the device)"
    exit 0
fi

echo "== pass 2: TensorRT engines (fresh torch-free process) =="
# Separate process so torch's ~2 GB is released before trtexec runs —
# on the shared-memory Orin Nano that headroom is what lets CUDA init.
"$CERAVIS_PYTHON" "$SETUP_DIR/export_models.py"

echo
echo "Engines in $(models_dir)/. Optional cleanup: rm -rf $VENV"
echo "A new ReID engine: restart ceravis, then re-enroll (or re-queue) a recipient."
