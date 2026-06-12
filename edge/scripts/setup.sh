#!/usr/bin/env bash
# =============================================================
# CERAVIS — ONE-COMMAND SETUP for the Jetson Orin Nano
# =============================================================
# Runs the entire bring-up end-to-end, in the right order:
#
#   [1/5] swap        — ensure >= 8 GB (protects compiles + engine builds)
#   [2/5] deps        — apt + pip runtime stack  (install_native.sh)
#   [3/5] engines     — YOLO26m detect+pose -> FP16 TRT (export_engines.sh)
#   [4/5] service     — systemd unit, start now + at boot (install_service.sh)
#   [5/5] doctor      — full verification gate    (check_jetson.sh)
#
# Every stage is idempotent: if it's already done, it's skipped in
# seconds. If a stage fails, fix and RE-RUN THIS SAME SCRIPT — completed
# work is never repeated, so no progress is lost.
#
# Run:  bash scripts/setup.sh
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

stage() { echo; echo "============ [$1] $2 ============"; }

# ---- [1/5] swap ---------------------------------------------------
stage "1/5" "swap"
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "${SWAP_MB:-0}" -ge 7000 ]; then
    echo "swap already ${SWAP_MB} MB — ok"
else
    if [ ! -f /swapfile ]; then
        echo "creating 8 GB /swapfile…"
        sudo fallocate -l 8G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
    fi
    sudo swapon /swapfile 2>/dev/null || true
    grep -q '^/swapfile' /etc/fstab \
        || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "swap now: $(free -h | awk '/^Swap:/{print $2}')"
fi

# ---- [2/5] dependencies -------------------------------------------
stage "2/5" "dependencies"
bash "$SCRIPTS_DIR/install_native.sh"

# ---- [3/5] engines -------------------------------------------------
stage "3/5" "TensorRT engines (YOLO26m detect + pose)"
bash "$SCRIPTS_DIR/export_engines.sh"

# ---- [4/5] service --------------------------------------------------
stage "4/5" "systemd service"
bash "$SCRIPTS_DIR/install_service.sh"
sudo systemctl restart ceravis

# ---- [5/5] doctor ----------------------------------------------------
stage "5/5" "verification"
bash "$SCRIPTS_DIR/check_jetson.sh" || {
    echo "Doctor reported failures — fix the [FAIL] lines and re-run setup.sh"
    exit 1
}

echo
echo "============================================================"
echo "  CERAVIS is up."
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "  Live Wall:   http://${IP:-<jetson-ip>}:8000/"
echo "  Setup:       http://${IP:-<jetson-ip>}:8000/ui/setup.html"
echo "  AI Monitor:  http://${IP:-<jetson-ip>}:8000/ui/monitor.html"
echo "  Logs:        journalctl -u ceravis -f"
echo "============================================================"
