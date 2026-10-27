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
# Offline: a release built on the PC carries wheels/ — every Python package for
# this device (arm64, Python 3.10), pycuda as source, and requirements.lock, the
# exact versions (setup/make_release.sh). With CERAVIS_WHEELS pointing there,
# pip installs ONLY from it: no internet, the same versions on every device. A
# system without python3-venv gets its environment from the release's
# vendor/virtualenv.pyz instead.
#
# Version lock otherwise: edge/requirements.lock (exact versions, `pip freeze`
# of a good install) is used as a constraints file when present. Without one,
# this install writes its own versions to <venv>/requirements.lock.
#
# Run:  bash setup/install_native.sh
# (Or run setup/setup.sh / setup/install.sh, which run everything end-to-end.)
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
CERAVIS_VENV="${CERAVIS_VENV:-}"
WHEELHOUSE="${CERAVIS_WHEELHOUSE:-}"      # keeps the compiled pycuda wheel (production)
WHEELS="${CERAVIS_WHEELS:-}"              # the release's own packages (offline)
# Arguments, not PIP_* variables: sudo drops the environment.
PIP_SRC=()
[ -n "$WHEELS" ] && [ -d "$WHEELS" ] && PIP_SRC=(--no-index --find-links "$WHEELS")

echo "== [1/4] apt packages (most are already present on JetPack) =="
# plugins-base/good: rtspsrc + depayloaders; plugins-bad: h264parse/h265parse
# (every camera's frames reach the AI through them); libav: avdec_* software-
# decode fallback. (plugins-ugly is encoders — not needed.) ffmpeg: audio in the
# recordings, fall-incident clips, snapshots from the recordings, ffprobe.
# python3-scipy comes from apt (prebuilt for aarch64, built against the system
# numpy 1.x) — avoids the pip "No module named scipy" failure and any numpy-2
# ABI clash. The BoT-SORT tracker uses scipy.optimize.linear_sum_assignment.
APT_PKGS=(python3-pip python3-venv python3-dev build-essential
          python3-numpy python3-scipy
          gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
          gstreamer1.0-plugins-bad gstreamer1.0-libav ffmpeg)
# OpenCV: JetPack 6.1+ already ships NVIDIA's own build (4.8, GStreamer on).
# Ubuntu's python3-opencv (4.5.4) next to it is a SECOND cv2 that can win the
# import — so it is added only when no usable cv2 (with GStreamer) is there.
python3 -c "import cv2, sys; sys.exit('GStreamer:                   YES' not in cv2.getBuildInformation())" \
    2>/dev/null || APT_PKGS+=(python3-opencv)
# TensorRT python bindings + trtexec (may be absent if SDK Manager's
# "Jetson SDK Components" were skipped during flashing).
python3 -c "import tensorrt" 2>/dev/null || APT_PKGS+=(python3-libnvinfer)
[ -x /usr/src/tensorrt/bin/trtexec ] || command -v trtexec >/dev/null 2>&1 \
    || APT_PKGS+=(libnvinfer-bin)
# The packages still missing, into MISSING. With the release's own Python
# packages, pip / venv / numpy / scipy come from them instead.
find_missing() {
    local p
    MISSING=()
    for p in "${APT_PKGS[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" && continue
        if [ ${#PIP_SRC[@]} -gt 0 ]; then
            case "$p" in python3-pip|python3-venv|python3-numpy|python3-scipy) continue ;; esac
        fi
        MISSING+=("$p")
    done
}
find_missing
# A release built on the PC carries the system packages this JetPack lacks
# (debs/, listed by the device itself and checked against its fingerprints):
# installed from there first. apt reads local files without any network (NOT
# --no-download: that accepts only apt's own download cache and refuses local
# files); it reaches the internet only for a dependency still missing, and
# never downgrades a newer package already installed.
DEBS="${CERAVIS_DEBS:-}"
if [ -z "${CERAVIS_SKIP_APT:-}" ] && [ "${#MISSING[@]}" -gt 0 ] && [ -n "$DEBS" ] \
        && ls "$DEBS"/*.deb >/dev/null 2>&1; then
    echo "   from the release: $(ls "$DEBS"/*.deb | wc -l) system package files (offline)"
    sudo apt-get install -y --no-install-recommends "$DEBS"/*.deb \
        || echo "   (the release's package files did not install — see above; trying the internet)"
    find_missing
fi
if [ -n "${CERAVIS_SKIP_APT:-}" ]; then
    echo "   skipped (an update: the system packages are already in place)"
elif [ "${#MISSING[@]}" -eq 0 ]; then
    echo "   all present — nothing to install (no internet needed)"
elif curl -fsI --max-time 8 http://ports.ubuntu.com >/dev/null 2>&1; then
    echo "   installing: ${MISSING[*]}"
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends "${MISSING[@]}"
else
    echo "ERROR: no internet, and these system packages are missing: ${MISSING[*]}" >&2
    echo "       connect the device to the internet once (Ethernet to the router, or the" >&2
    echo "       laptop's shared connection) and run the installer again." >&2
    exit 1
fi

# The release's virtualenv: Python's own venv when the system has it
# (python3-venv), else the release's vendor/virtualenv.pyz, which brings pip
# with it — either way it sees JetPack's numpy / OpenCV / TensorRT.
make_venv() {
    if python3 -c "import ensurepip" 2>/dev/null; then
        sudo python3 -m venv --system-site-packages "$1"
    elif [ -s "$CERAVIS_ROOT/vendor/virtualenv.pyz" ]; then
        sudo python3 "$CERAVIS_ROOT/vendor/virtualenv.pyz" --system-site-packages \
            --no-periodic-update -q "$1"
    else
        echo "ERROR: no python3-venv and no vendor/virtualenv.pyz — cannot make $1" >&2
        return 1
    fi
}

if [ -n "$CERAVIS_VENV" ]; then
    [ -x "$CERAVIS_VENV/bin/python" ] || make_venv "$CERAVIS_VENV"
    PY="$CERAVIS_VENV/bin/python"
    # No cache: nothing of the install is left in /root (the master image).
    PIP=(sudo "$PY" -m pip --no-cache-dir)
    PIP_TARGET="the release virtualenv ($CERAVIS_VENV)"
else
    PY=python3
    PIP=(pip3)
    PIP_USER=(--user)
    PIP_TARGET="system python, --user"
fi
CONSTRAINTS=()
if [ ${#PIP_SRC[@]} -gt 0 ] && [ -s "$WHEELS/requirements.lock" ]; then
    CONSTRAINTS=(-c "$WHEELS/requirements.lock")
elif [ -s "$EDGE_DIR/requirements.lock" ]; then
    CONSTRAINTS=(-c "$EDGE_DIR/requirements.lock")
fi

FROM=""
[ ${#PIP_SRC[@]} -gt 0 ] && FROM=", from the release packages (offline)"
echo "== [2/4] pip runtime deps ($PIP_TARGET$FROM) =="
# requirements.txt pins numpy<2 — a pip numpy 2.x shadows JetPack's numpy and
# breaks apt-opencv's ABI ("import cv2" fails). This both prevents and REPAIRS
# that state (downgrades a stray 2.x).
"${PIP[@]}" install ${PIP_USER[@]+"${PIP_USER[@]}"} ${PIP_SRC[@]+"${PIP_SRC[@]}"} \
    -r "$EDGE_DIR/requirements.txt" ${CONSTRAINTS[@]+"${CONSTRAINTS[@]}"}

echo "== [3/4] pycuda (compiles against /usr/local/cuda, one-time ~5-10 min) =="
export PATH="/usr/local/cuda/bin:$PATH"
export CUDA_ROOT="/usr/local/cuda"
if ! "$PY" -c "import pycuda" 2>/dev/null; then
    if [ -n "$WHEELHOUSE" ]; then
        # Compiled ONCE into the wheelhouse; every later release installs that
        # wheel in seconds instead of compiling again — the version the lock
        # pins, so an older release's wheel is never taken for it.
        PYCUDA_V=""
        [ ${#CONSTRAINTS[@]} -gt 0 ] \
            && PYCUDA_V="$(grep -i '^pycuda==' "${CONSTRAINTS[1]}" | cut -d= -f3 | tr -d '\r')"
        sudo mkdir -p "$WHEELHOUSE"
        ls "$WHEELHOUSE"/pycuda-"${PYCUDA_V:-}"*.whl >/dev/null 2>&1 \
            || sudo env PATH="$PATH" CUDA_ROOT="$CUDA_ROOT" "$PY" -m pip --no-cache-dir wheel --no-deps \
                   ${PIP_SRC[@]+"${PIP_SRC[@]}"} ${CONSTRAINTS[@]+"${CONSTRAINTS[@]}"} pycuda -w "$WHEELHOUSE"
        # Its runtime packages (pytools, mako, …) from the same source as the rest.
        "${PIP[@]}" install ${PIP_SRC[@]+"${PIP_SRC[@]}"} --find-links "$WHEELHOUSE" \
            ${CONSTRAINTS[@]+"${CONSTRAINTS[@]}"} pycuda
    else
        "${PIP[@]}" install ${PIP_USER[@]+"${PIP_USER[@]}"} ${PIP_SRC[@]+"${PIP_SRC[@]}"} pycuda
    fi
fi

# scipy powers the BoT-SORT tracker's data association. apt usually provides it
# (prebuilt aarch64); if that package didn't land, fall back to a pip wheel
# (scipy<1.14 stays numpy-1.x compatible). Gate before continuing.
"$PY" -c "import scipy" 2>/dev/null \
    || "${PIP[@]}" install ${PIP_USER[@]+"${PIP_USER[@]}"} ${PIP_SRC[@]+"${PIP_SRC[@]}"} \
           ${CONSTRAINTS[@]+"${CONSTRAINTS[@]}"} "scipy<1.14"
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
