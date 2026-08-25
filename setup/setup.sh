#!/usr/bin/env bash
# =============================================================
# CERAVIS — ONE-COMMAND SETUP for the Jetson Orin Nano
# =============================================================
# Takes a bare device to a running, fleet-connected edge unit:
#
#   [1/6] deps        — apt + pip stack, MediaMTX, hotspot
#   [2/6] engines     — YOLO26m detect+pose AND the ReID engine -> FP16 TRT
#   [3/6] tunnel      — frpc + the privileged apply-edge-id helper
#   [4/6] services    — ceravis + the nightly reboot timer, started and enabled
#   [5/6] reboot pw   — password for the MANUAL reboot endpoint (optional)
#   [6/6] doctor      — full verification gate
#
# Every stage is idempotent: already-done work is skipped in seconds. If a
# stage fails, fix it and RE-RUN THIS SAME SCRIPT — nothing completed is
# repeated, so no progress is lost.
#
# Run:  bash setup/setup.sh
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPTS_DIR")"

stage() { echo; echo "============ [$1] $2 ============"; }
skip()  { echo "  [skip] $1"; }

# ---- [1/6] dependencies ---------------------------------------------
stage "1/6" "dependencies"
bash "$SCRIPTS_DIR/install_native.sh"
bash "$SCRIPTS_DIR/install_mediamtx.sh"
bash "$SCRIPTS_DIR/install_hotspot.sh"

# ---- [2/6] engines ---------------------------------------------------
# Both model families, not just detection: without the ReID engine the device
# tracks but never IDENTIFIES anyone, and the whole AI layer stays gated —
# silently, because appearance-off is only an INFO line at startup.
stage "2/6" "TensorRT engines (YOLO26m detect + pose, ReID)"
bash "$SCRIPTS_DIR/export_engines.sh"
bash "$SCRIPTS_DIR/export_reid.sh"

# ---- [3/6] fleet tunnel ----------------------------------------------
# Installs frpc AND the locked-down apply-edge-id helper. That helper is what
# lets the device push its own edge_id into frpc.toml at account verification;
# without it the tunnel never learns the token and every live link stays dead.
stage "3/6" "fleet tunnel (frpc)"
if [ -f "$REPO_DIR/cloud/install_frpc.sh" ]; then
    bash "$REPO_DIR/cloud/install_frpc.sh"
else
    skip "cloud/install_frpc.sh not present — LAN-only device"
fi

# ---- [4/6] services ---------------------------------------------------
stage "4/6" "systemd services"
bash "$SCRIPTS_DIR/install_service.sh"
bash "$SCRIPTS_DIR/install_reboot_timer.sh"
sudo systemctl restart ceravis

# ---- [5/6] reboot password --------------------------------------------
# Guards the MANUAL reboot endpoint only; the nightly timer needs no password.
# Skipped without prompting on a non-interactive run (CI, remote provisioning),
# so setup never hangs waiting for a terminal that isn't there.
stage "5/6" "manual-reboot password"
if sudo -n test -f "$REPO_DIR/edge/data/reboot_auth.json" 2>/dev/null \
   || [ -f "$REPO_DIR/edge/data/reboot_auth.json" ]; then
    skip "already set (re-run setup/set_reboot_password.py to change it)"
elif [ -t 0 ]; then
    python3 "$SCRIPTS_DIR/set_reboot_password.py" || \
        echo "  (skipped — set it later with setup/set_reboot_password.py)"
else
    skip "non-interactive shell — run setup/set_reboot_password.py later"
fi

# ---- [6/6] doctor ------------------------------------------------------
stage "6/6" "verification"
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
echo
echo "  NEXT: register the account at /ui/setup.html. The device stores its"
echo "  edge_id in data/account.json, pushes it into cloud/frpc.toml and"
echo "  restarts the tunnel itself — no manual step, no restart of ceravis."
echo "============================================================"
