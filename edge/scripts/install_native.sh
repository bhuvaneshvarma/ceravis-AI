#!/usr/bin/env bash
# =============================================================
# CERAVIS native install — Jetson Orin Nano, JetPack 6.x
# =============================================================
# Uses JetPack's OWN stack: CUDA, TensorRT (+ python bindings),
# OpenCV (GStreamer build), GStreamer. Only small pure-python deps
# come from pip. Idempotent — safe to re-run anytime.
#
# Run:  bash scripts/install_native.sh
# (Or just run scripts/setup.sh, which runs everything end-to-end.)
set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== [1/5] apt packages (most are already present on JetPack) =="
sudo apt-get update
# plugins-base/good: rtspsrc + depayloaders; plugins-bad: h264parse/h265parse;
# libav: avdec_* software-decode fallback. (plugins-ugly is encoders — not needed.)
# python3-scipy + python3-matplotlib come from apt (prebuilt for aarch64,
# built against the system numpy 1.x) — avoids the pip "No module named
# scipy" failure and any numpy-2 ABI clash. supervision needs both.
sudo apt-get install -y --no-install-recommends \
    python3-pip python3-venv python3-dev build-essential \
    python3-numpy python3-opencv python3-scipy python3-matplotlib \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav

# TensorRT python bindings + trtexec (may be absent if SDK Manager's
# "Jetson SDK Components" were skipped during flashing).
python3 -c "import tensorrt" 2>/dev/null \
    || sudo apt-get install -y python3-libnvinfer
[ -x /usr/src/tensorrt/bin/trtexec ] || command -v trtexec >/dev/null 2>&1 \
    || sudo apt-get install -y libnvinfer-bin

echo "== [2/5] pip runtime deps (system python, --user) =="
# requirements.txt pins numpy<2 — a pip numpy 2.x in ~/.local shadows
# JetPack's numpy and breaks apt-opencv's ABI ("import cv2" fails).
# This both prevents and REPAIRS that state (downgrades a stray 2.x).
pip3 install --user -r "$EDGE_DIR/requirements.txt"

echo "== [3/5] pycuda (compiles against /usr/local/cuda, one-time ~5-10 min) =="
export PATH="/usr/local/cuda/bin:$PATH"
export CUDA_ROOT="/usr/local/cuda"
python3 -c "import pycuda" 2>/dev/null || pip3 install --user pycuda

# scipy + matplotlib are supervision's hard deps. apt usually provides them
# (prebuilt aarch64), but if that package didn't land, fall back to pip wheels
# (scipy<1.14 stays numpy-1.x compatible). Guarantees "import scipy" works.
python3 -c "import scipy, matplotlib" 2>/dev/null \
    || pip3 install --user "scipy<1.14" matplotlib

# supervision is installed WITHOUT deps so pip cannot pull opencv-python /
# numpy wheels that would shadow JetPack's GStreamer-enabled builds.
# (Its actual deps are already covered above.)
python3 -c "import supervision" 2>/dev/null \
    || pip3 install --user --no-deps supervision

# Final dep gate: supervision must import (pulls in scipy) before we continue.
python3 -c "import supervision" || {
    echo "ERROR: supervision/scipy still not importable — see messages above"; exit 1; }

echo "== [4/5] data files =="
if [ ! -f "$EDGE_DIR/data/cameras.json" ]; then
    cp "$EDGE_DIR/data/cameras.example.json" "$EDGE_DIR/data/cameras.json"
    echo ">> Edit $EDGE_DIR/data/cameras.json with your real RTSP URL(s)"
fi

echo "== [5/5] verify (run LAST so pip-stage breakage is caught) =="
python3 - <<'PY'
import numpy, cv2, tensorrt, pycuda, supervision
assert numpy.__version__.startswith("1."), \
    f"numpy {numpy.__version__} is 2.x — apt-opencv needs 1.x (re-run this script)"
gst = "GStreamer:                   YES" in cv2.getBuildInformation()
print("numpy      ", numpy.__version__)
print("opencv     ", cv2.__version__, "| GStreamer:", "YES" if gst else "NO (check)")
print("tensorrt   ", tensorrt.__version__)
print("supervision", supervision.__version__)
print("ALL RUNTIME DEPS OK")
PY

echo
echo "install_native: done."
