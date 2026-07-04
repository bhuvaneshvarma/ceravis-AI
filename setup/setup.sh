#!/usr/bin/env bash
# =============================================================
# CERAVIS — ONE-COMMAND SETUP for the Jetson Orin Nano
# =============================================================
# Runs the entire bring-up end-to-end, in the right order:
#
#   [1/4] deps        — apt + pip runtime stack (install_native.sh)
#                       + MediaMTX media backbone (install_mediamtx.sh)
#   [2/4] engines     — YOLO26m detect+pose -> FP16 TRT (export_engines.sh)
#   [3/4] service     — systemd unit, start now + at boot (install_service.sh)
#   [4/4] doctor      — full verification gate    (check_jetson.sh)
#
# Every stage is idempotent: if it's already done, it's skipped in
# seconds. If a stage fails, fix and RE-RUN THIS SAME SCRIPT — completed
# work is never repeated, so no progress is lost.
#
# Run:  bash setup/setup.sh
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

stage() { echo; echo "============ [$1] $2 ============"; }

# ---- [1/4] dependencies -------------------------------------------
stage "1/4" "dependencies"
bash "$SCRIPTS_DIR/install_native.sh"
bash "$SCRIPTS_DIR/install_mediamtx.sh"
bash "$SCRIPTS_DIR/install_hotspot.sh"

# ---- [2/4] engines -------------------------------------------------
stage "2/4" "TensorRT engines (YOLO26m detect + pose)"
bash "$SCRIPTS_DIR/export_engines.sh"

# ---- [3/4] service --------------------------------------------------
stage "3/4" "systemd service"
bash "$SCRIPTS_DIR/install_service.sh"
sudo systemctl restart ceravis

# ---- [4/4] doctor ----------------------------------------------------
stage "4/4" "verification"
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
