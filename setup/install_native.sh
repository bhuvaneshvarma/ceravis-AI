#!/usr/bin/env bash
# =============================================================
# CERAVIS native install — Jetson Orin Nano, JetPack 6.x
# =============================================================
# Uses JetPack's OWN stack: CUDA, TensorRT (+ python bindings),
# OpenCV (GStreamer build), GStreamer. Only small pure-python deps
# come from pip. Idempotent — safe to re-run anytime.
#
# Where the pip packages go (setup/lib.sh):
#   development  pip --user for the installing account (as before)
#   production   CERAVIS_VENV — the release's own virtualenv, which still sees
#                JetPack's numpy / OpenCV / TensorRT (--system-site-packages)
#
# Version lock: edge/requirements.lock (exact versions, `pip freeze` of a good
# install) is used as a constraints file when present, so every device gets the
# same versions. Without one, this install writes its own versions to
# <venv>/requirements.lock — copy that into edge/ to pin the next ones.
#
# Run:  bash setup/install_native.sh
# (Or run setup/setup.sh / setup/install.sh, which run everything end-to-end.)
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
CERAVIS_VENV="${CERAVIS_VENV:-}"
WHEELHOUSE="${CERAVIS_WHEELHOUSE:-}"      # keeps the compiled pycuda wheel (production)

echo "== [1/4] apt packages (most are already present on JetPack) =="
if [ -n "${CERAVIS_SKIP_APT:-}" ]; then
    echo "   skipped (an update: the system packages are already in place)"
else
    sudo apt-get update
    # plugins-base/good: rtspsrc + depayloaders; plugins-bad: h264parse/h265parse;
    # libav: avdec_* software-decode fallback. (plugins-ugly is encoders — not needed.)
    # python3-scipy comes from apt (prebuilt for aarch64, built against the system
    # numpy 1.x) — avoids the pip "No module named scipy" failure and any numpy-2
    # ABI clash. The BoT-SORT tracker uses scipy.optimize.linear_sum_assignment.
    sudo apt-get install -y --no-install-recommends \
        python3-pip python3-venv python3-dev build-essential \
        python3-numpy python3-opencv python3-scipy \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-libav

    # TensorRT python bindings + trtexec (may be absent if SDK Manager's
    # "Jetson SDK Components" were skipped during flashing).
    python3 -c "import tensorrt" 2>/dev/null \
        || sudo apt-get install -y python3-libnvinfer
    [ -x /usr/src/tensorrt/bin/trtexec ] || command -v trtexec >/dev/null 2>&1 \
        || sudo apt-get install -y libnvinfer-bin
fi

if [ -n "$CERAVIS_VENV" ]; then
    [ -x "$CERAVIS_VENV/bin/python" ] || sudo python3 -m venv --system-site-packages "$CERAVIS_VENV"
    PY="$CERAVIS_VENV/bin/python"
    PIP=(sudo "$PY" -m pip)
    PIP_TARGET="the release virtualenv ($CERAVIS_VENV)"
else
    PY=python3
    PIP=(pip3)
    PIP_USER=(--user)
    PIP_TARGET="system python, --user"
fi
CONSTRAINTS=()
[ -s "$EDGE_DIR/requirements.lock" ] && CONSTRAINTS=(-c "$EDGE_DIR/requirements.lock")

echo "== [2/4] pip runtime deps ($PIP_TARGET) =="
# requirements.txt pins numpy<2 — a pip numpy 2.x shadows JetPack's numpy and
# breaks apt-opencv's ABI ("import cv2" fails). This both prevents and REPAIRS
# that state (downgrades a stray 2.x).
"${PIP[@]}" install ${PIP_USER[@]+"${PIP_USER[@]}"} -r "$EDGE_DIR/requirements.txt" \
    ${CONSTRAINTS[@]+"${CONSTRAINTS[@]}"}

echo "== [3/4] pycuda (compiles against /usr/local/cuda, one-time ~5-10 min) =="
export PATH="/usr/local/cuda/bin:$PATH"
export CUDA_ROOT="/usr/local/cuda"
if ! "$PY" -c "import pycuda" 2>/dev/null; then
    if [ -n "$WHEELHOUSE" ]; then
        # Compiled ONCE into the wheelhouse; every later release installs that
        # wheel in seconds instead of compiling again.
        sudo mkdir -p "$WHEELHOUSE"
        ls "$WHEELHOUSE"/pycuda-*.whl >/dev/null 2>&1 \
            || sudo env PATH="$PATH" CUDA_ROOT="$CUDA_ROOT" "$PY" -m pip wheel --no-deps pycuda -w "$WHEELHOUSE"
        "${PIP[@]}" install --no-index --find-links "$WHEELHOUSE" pycuda
    else
        "${PIP[@]}" install ${PIP_USER[@]+"${PIP_USER[@]}"} pycuda
    fi
fi

# scipy powers the BoT-SORT tracker's data association. apt usually provides it
# (prebuilt aarch64); if that package didn't land, fall back to a pip wheel
# (scipy<1.14 stays numpy-1.x compatible). Gate before continuing.
"$PY" -c "import scipy" 2>/dev/null \
    || "${PIP[@]}" install ${PIP_USER[@]+"${PIP_USER[@]}"} "scipy<1.14"
"$PY" -c "import scipy" || {
    echo "ERROR: scipy still not importable — see messages above"; exit 1; }

if [ -n "$CERAVIS_VENV" ] && [ ! -s "$EDGE_DIR/requirements.lock" ]; then
    "$PY" -m pip freeze --local | sudo tee "$CERAVIS_VENV/requirements.lock" >/dev/null
    echo "   versions recorded: $CERAVIS_VENV/requirements.lock (copy to edge/ to pin)"
fi

echo "== [4/4] verify (run LAST so pip-stage breakage is caught) =="
"$PY" - <<'PY'
import numpy, cv2, tensorrt, pycuda, scipy
assert numpy.__version__.startswith("1."), \
    f"numpy {numpy.__version__} is 2.x — apt-opencv needs 1.x (re-run this script)"
gst = "GStreamer:                   YES" in cv2.getBuildInformation()
print("numpy      ", numpy.__version__)
print("opencv     ", cv2.__version__, "| GStreamer:", "YES" if gst else "NO (check)")
print("tensorrt   ", tensorrt.__version__)
print("scipy      ", scipy.__version__)
print("ALL RUNTIME DEPS OK")
PY

echo
echo "install_native: done."
