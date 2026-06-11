#!/usr/bin/env bash
# =============================================================
# CERAVIS native install — Jetson Orin Nano, JetPack 6.x
# =============================================================
# Uses JetPack's OWN stack: CUDA, TensorRT (+ python bindings),
# OpenCV (GStreamer build), GStreamer, numpy. No Docker, no duplicate
# CUDA/TensorRT downloads. Only small pure-python deps come from pip.
#
# Run once:  bash scripts/install_native.sh
set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== [1/5] apt packages (most are already present on JetPack) =="
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    python3-pip python3-venv python3-dev build-essential \
    python3-numpy python3-opencv \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav

echo "== [2/5] verify the JetPack stack =="
# TensorRT python bindings ship with the JetPack SDK components;
# install them from NVIDIA's apt repo if missing.
python3 -c "import tensorrt" 2>/dev/null \
    || sudo apt-get install -y python3-libnvinfer
python3 - <<'PY'
import numpy, cv2, tensorrt
print("numpy   ", numpy.__version__)
print("opencv  ", cv2.__version__,
      "| GStreamer:", "YES" if "GStreamer:                   YES"
      in cv2.getBuildInformation() else "NO (check)")
print("tensorrt", tensorrt.__version__)
PY

echo "== [3/5] pip runtime deps (system python, --user) =="
pip3 install --user -r "$EDGE_DIR/requirements.txt"

echo "== [4/5] pycuda (compiles against /usr/local/cuda, one-time ~5-10 min) =="
export PATH="/usr/local/cuda/bin:$PATH"
export CUDA_ROOT="/usr/local/cuda"
python3 -c "import pycuda" 2>/dev/null || pip3 install --user pycuda

# supervision is installed WITHOUT deps so pip cannot pull opencv-python /
# numpy wheels that would shadow JetPack's GStreamer-enabled builds.
pip3 install --user --no-deps supervision

echo "== [5/5] data files =="
if [ ! -f "$EDGE_DIR/data/cameras.json" ]; then
    cp "$EDGE_DIR/data/cameras.example.json" "$EDGE_DIR/data/cameras.json"
    echo ">> Edit $EDGE_DIR/data/cameras.json with your real RTSP URL(s)"
fi

echo
echo "Done. Next steps:"
echo "  1. bash scripts/export_engines.sh        # build TensorRT engines (one-time)"
echo "  2. bash scripts/install_service.sh       # run at boot via systemd"
echo "     — or run manually:"
echo "       cd $EDGE_DIR && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000"
