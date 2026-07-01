#!/usr/bin/env bash
# =============================================================
# CERAVIS Jetson doctor — verifies every dependency, with fixes.
# Read-only: changes nothing on the system.
#
# Run:  bash setup/check_jetson.sh
# Exit code 0 = ready to run CERAVIS; 1 = something needs fixing.
# =============================================================

PASS=0; FAIL=0; WARN=0

ok()   { echo "  [ OK ] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; [ -n "$2" ] && echo "         fix:  $2"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $1"; [ -n "$2" ] && echo "         note: $2"; WARN=$((WARN+1)); }

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
EDGE_DIR="$REPO_DIR/edge"

echo "================ CERAVIS Jetson doctor ================"

# ---- 1. Platform ------------------------------------------------
echo "-- platform"
if [ -f /etc/nv_tegra_release ]; then
    L4T="$(head -c 60 /etc/nv_tegra_release)"
    case "$L4T" in
        *R36*) ok "L4T R36.x / JetPack 6.x  ($(echo "$L4T" | grep -o 'R36[^,]*'))" ;;
        *)     warn "Unexpected L4T: $L4T" "CERAVIS targets JetPack 6.x (R36)" ;;
    esac
else
    bad "Not a Jetson? /etc/nv_tegra_release missing"
fi

# ---- 2. CUDA toolkit (nvcc — needed once, to compile pycuda) ----
echo "-- cuda"
NVCC=""
command -v nvcc >/dev/null 2>&1 && NVCC="nvcc"
[ -z "$NVCC" ] && [ -x /usr/local/cuda/bin/nvcc ] && NVCC="/usr/local/cuda/bin/nvcc"
if [ -n "$NVCC" ]; then
    ok "CUDA toolkit ($("$NVCC" --version | grep -o 'release [0-9.]*' | head -1))"
else
    bad "nvcc not found (SDK Components were likely skipped in SDK Manager)" \
        "sudo apt update && sudo apt install -y cuda-toolkit-12-6"
fi

# ---- 3. TensorRT ------------------------------------------------
echo "-- tensorrt"
TRT_VER="$(python3 -c 'import tensorrt; print(tensorrt.__version__)' 2>/dev/null)"
if [ -n "$TRT_VER" ]; then
    case "$TRT_VER" in
        10.*) ok "TensorRT python $TRT_VER (TRT10 — app uses execute_async_v3)" ;;
        8.*)  ok "TensorRT python $TRT_VER (TRT8 — app falls back to v2 API)" ;;
        *)    warn "TensorRT python $TRT_VER (untested major version)" ;;
    esac
else
    bad "python3 cannot import tensorrt" \
        "sudo apt update && sudo apt install -y python3-libnvinfer"
fi
if command -v trtexec >/dev/null 2>&1 || [ -x /usr/src/tensorrt/bin/trtexec ]; then
    ok "trtexec present (builds the FP16 engines)"
else
    bad "trtexec not found" \
        "sudo apt update && sudo apt install -y libnvinfer-bin   (or: sudo apt install -y nvidia-jetpack)"
fi

# ---- 4. OpenCV + numpy ------------------------------------------
echo "-- opencv / numpy"
CV="$(python3 - 2>/dev/null <<'PY'
import cv2
gst = "GStreamer:                   YES" in cv2.getBuildInformation()
print(cv2.__version__, "gst" if gst else "nogst")
PY
)"
if [ -n "$CV" ]; then
    case "$CV" in
        *gst)   ok "OpenCV ${CV% *} with GStreamer" ;;
        *nogst) warn "OpenCV ${CV% *} WITHOUT GStreamer" \
                     "hardware decode path unavailable; FFmpeg fallback will be used" ;;
    esac
else
    # Most common cause: a pip numpy 2.x in ~/.local shadowing JetPack's
    # numpy 1.x — apt-opencv is built against the 1.x ABI.
    bad "python3 cannot import cv2" \
        "pip3 install --user 'numpy<2' && sudo apt install -y python3-opencv"
fi
NP="$(python3 -c 'import numpy; print(numpy.__version__)' 2>/dev/null)"
[ -n "$NP" ] && ok "numpy $NP" || bad "numpy missing" "sudo apt install -y python3-numpy"

# ---- 5. GStreamer elements --------------------------------------
echo "-- gstreamer"
if command -v gst-inspect-1.0 >/dev/null 2>&1; then
    gst-inspect-1.0 nvv4l2decoder >/dev/null 2>&1 \
        && ok "nvv4l2decoder (hardware decode)" \
        || warn "nvv4l2decoder missing" "software/FFmpeg fallback will be used"
    gst-inspect-1.0 avdec_h264 >/dev/null 2>&1 \
        && ok "avdec_h264 (software decode fallback)" \
        || bad "avdec_h264 missing" "sudo apt install -y gstreamer1.0-libav"
else
    bad "gst-inspect-1.0 missing" "sudo apt install -y gstreamer1.0-tools"
fi

# ---- 6. Python runtime deps -------------------------------------
echo "-- python runtime deps (pip)"
for mod in fastapi uvicorn pydantic pydantic_settings websockets \
           requests faiss scipy pycuda; do
    if python3 -c "import $mod" 2>/dev/null; then
        ok "$mod"
    else
        case "$mod" in
            pycuda) F="bash setup/install_native.sh   (compiles pycuda)" ;;
            scipy)  F="sudo apt-get install -y python3-scipy" ;;
            *)      F="pip3 install --user -r requirements.txt" ;;
        esac
        bad "python module '$mod' missing" "$F"
    fi
done

# ---- 7. Models ----------------------------------------------------
echo "-- models"
for m in detection/yolo26m pose/yolo26m-pose; do
    if [ -s "$EDGE_DIR/models/$m.engine" ]; then
        ok "$m.engine ($(du -h "$EDGE_DIR/models/$m.engine" | cut -f1))"
    elif [ -s "$EDGE_DIR/models/$m.onnx" ]; then
        warn "$m.onnx present but engine not built" "python3 setup/export_models.py"
    else
        warn "$m not exported yet" "bash setup/export_engines.sh"
    fi
done

# ---- 8. System health ---------------------------------------------
echo "-- system"
command -v tegrastats >/dev/null 2>&1 && ok "tegrastats (dashboard metrics)" \
    || warn "tegrastats missing" "system metrics card will be empty"
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "${SWAP_MB:-0}" -ge 3000 ]; then ok "swap ${SWAP_MB} MB (zram default is fine)"
else warn "swap is ${SWAP_MB:-0} MB" "only matters if a build gets OOM-killed; then add a swapfile"; fi
DISK=$(df -h / | awk 'NR==2{print $4}')
ok "free disk on /: $DISK"
if command -v nvpmodel >/dev/null 2>&1; then
    MODE="$(sudo -n nvpmodel -q 2>/dev/null | head -1 || nvpmodel -q 2>/dev/null | head -1)"
    [ -n "$MODE" ] && ok "power mode: $MODE" || warn "couldn't query nvpmodel" "sudo nvpmodel -q"
fi

# ---- 9. Service ----------------------------------------------------
echo "-- service"
if systemctl list-unit-files 2>/dev/null | grep -q '^ceravis.service'; then
    STATE="$(systemctl is-active ceravis 2>/dev/null)"
    [ "$STATE" = "active" ] && ok "ceravis.service active" \
        || warn "ceravis.service installed but $STATE" "sudo systemctl restart ceravis && journalctl -u ceravis -n 30"
else
    warn "ceravis.service not installed" "bash setup/install_service.sh"
fi

# ---- summary -------------------------------------------------------
echo "======================================================="
echo "  $PASS ok · $WARN warnings · $FAIL failures"
if [ "$FAIL" -gt 0 ]; then
    echo "  Fix the [FAIL] items above, then re-run this script."
    exit 1
fi
echo "  System is ready."
