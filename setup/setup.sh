#!/usr/bin/env bash
# =============================================================
# CERAVIS — ONE-COMMAND SETUP for the Jetson Orin Nano
# =============================================================
# Takes a bare device to a running, fleet-connected edge unit:
#
#   [1/7] config      — generate infra/env/jetson.env from the template
#   [2/7] deps        — apt + pip stack, MediaMTX, hotspot
#   [3/7] engines     — YOLO26m detect+pose AND the ReID engine -> FP16 TRT
#   [4/7] tunnel      — frpc + the privileged apply-edge-id helper
#   [5/7] services    — ceravis + the nightly reboot timer, started and enabled
#   [6/7] reboot pw   — password for the MANUAL reboot endpoint (optional)
#   [7/7] doctor      — full verification gate
#
# Every stage is idempotent: already-done work is skipped in seconds. If a
# stage fails, fix it and RE-RUN THIS SAME SCRIPT — nothing completed is
# repeated, so no progress is lost.
#
# Run:  bash setup/setup.sh
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPTS_DIR")"
ENV_DIR="$REPO_DIR/edge/infra/env"

stage() { echo; echo "============ [$1] $2 ============"; }
skip()  { echo "  [skip] $1"; }

# ---- [1/7] config ---------------------------------------------------
# FIRST, because every stage below reads it. jetson.env is gitignored and the
# device WRITES to it (EDGE_ID at account verification) — a tracked file the
# device rewrites is what makes `git pull` abort on every later update.
stage "1/7" "device config"
if [ -f "$ENV_DIR/jetson.env" ]; then
    skip "jetson.env already exists — left exactly as it is"
else
    cp "$ENV_DIR/jetson.env.example" "$ENV_DIR/jetson.env"
    echo "  created $ENV_DIR/jetson.env from the template"
fi

# ---- [2/7] dependencies ---------------------------------------------
stage "2/7" "dependencies"
bash "$SCRIPTS_DIR/install_native.sh"
bash "$SCRIPTS_DIR/install_mediamtx.sh"
bash "$SCRIPTS_DIR/install_hotspot.sh"

# ---- [3/7] engines ---------------------------------------------------
# Both model families, not just detection: without the ReID engine the device
# tracks but never IDENTIFIES anyone, and the whole AI layer stays gated —
# silently, because appearance-off is only an INFO line at startup.
stage "3/7" "TensorRT engines (YOLO26m detect + pose, ReID)"
bash "$SCRIPTS_DIR/export_engines.sh"
bash "$SCRIPTS_DIR/export_reid.sh"

# ---- [4/7] fleet tunnel ----------------------------------------------
# Installs frpc AND the locked-down apply-edge-id helper. That helper is what
# lets the device push its own edge_id into frpc.toml at account verification;
# without it the tunnel never learns the token and every live link stays dead.
stage "4/7" "fleet tunnel (frpc)"
if [ -f "$REPO_DIR/cloud/install_frpc.sh" ]; then
    bash "$REPO_DIR/cloud/install_frpc.sh"
else
    skip "cloud/install_frpc.sh not present — LAN-only device"
fi

# ---- [5/7] services ---------------------------------------------------
stage "5/7" "systemd services"
bash "$SCRIPTS_DIR/install_service.sh"
bash "$SCRIPTS_DIR/install_reboot_timer.sh"
sudo systemctl restart ceravis

# ---- [6/7] reboot password --------------------------------------------
# Guards the MANUAL reboot endpoint only; the nightly timer needs no password.
# Skipped without prompting on a non-interactive run (CI, remote provisioning),
# so setup never hangs waiting for a terminal that isn't there.
stage "6/7" "manual-reboot password"
if sudo -n test -f "$REPO_DIR/edge/data/reboot_auth.json" 2>/dev/null \
   || [ -f "$REPO_DIR/edge/data/reboot_auth.json" ]; then
    skip "already set (re-run setup/set_reboot_password.py to change it)"
elif [ -t 0 ]; then
    python3 "$SCRIPTS_DIR/set_reboot_password.py" || \
        echo "  (skipped — set it later with setup/set_reboot_password.py)"
else
    skip "non-interactive shell — run setup/set_reboot_password.py later"
fi

# ---- [7/7] doctor ------------------------------------------------------
stage "7/7" "verification"
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
echo "  NEXT: register the account at /ui/setup.html. The device then writes"
echo "  its edge_id into jetson.env AND cloud/frpc.toml and restarts the"
echo "  tunnel by itself — no manual step, no restart of ceravis needed."
echo "============================================================"
