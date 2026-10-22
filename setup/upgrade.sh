#!/usr/bin/env bash
# =============================================================
# CERAVIS one-shot upgrade — OSNet ReID, FastReid cleanup, scipy fix
# =============================================================
# Pulls latest code, purges every FastReid leftover + stale caches,
# repairs dependencies (scipy/matplotlib via apt), builds the OSNet ReID
# engine, and restarts the service. Idempotent — safe to re-run.
#
# Run:  bash setup/upgrade.sh
#
# After its pull it starts again on the NEW copy of itself, so steps the pull
# brings run on this upgrade. A device whose copy predates that runs its OLD
# steps on the upgrade that brings it: run `bash setup/upgrade.sh` once more
# there (check_jetson.sh at the end flags a fleet agent that still needs it).
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
EDGE_DIR="$REPO_DIR/edge"
cd "$EDGE_DIR"

# bash goes on reading the copy of this file it started with — the pull must
# not leave the rest of the upgrade running the old steps. Once (env guard).
if [ -z "${CERAVIS_UPGRADE_PULLED:-}" ]; then
    echo "== [1/6] latest code =="
    git -C "$REPO_DIR" pull --ff-only || echo "  (pull skipped — resolve manually if needed)"
    CERAVIS_UPGRADE_PULLED=1 exec bash "$SETUP_DIR/upgrade.sh" "$@"
fi

echo "== [2/6] purge FastReid leftovers + stale caches =="
rm -f models/reid/fastreid* models/reid/*fast* 2>/dev/null || true
rm -rf /tmp/ceravis-export-venv          # rebuilt clean for OSNet below
pip3 cache purge 2>/dev/null || true
find "$EDGE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
sudo apt-get autoremove -y >/dev/null 2>&1 || true
echo "  cleaned."

echo "== [3/6] dependencies (scipy/matplotlib via apt fixes 'No module scipy') =="
bash "$SETUP_DIR/install_native.sh"

echo "== [4/6] build OSNet ReID engine =="
bash "$SETUP_DIR/export_reid.sh"

echo "== [5/6] restart service + fleet agent =="
# The app-server API key lives in the root-only /etc/ceravis/secrets.env, not in
# the public repo: asked for once (a stored key is kept), then the unit is
# re-installed so the /etc copy loads that file — both before the restart that
# has to pick it up.
bash "$SETUP_DIR/install_api_key.sh"
bash "$SETUP_DIR/install_service.sh"
sudo systemctl restart ceravis
# Re-installs the agent's unit (it now loads the root-only enroll.env) and asks
# once for the fleet enrollment key — the key left jetson.env, and without it
# the device cannot re-enroll after a Reset key. Keeps a key already stored.
bash "$SETUP_DIR/install_fleet_agent.sh"
sleep 6

echo "== [6/6] verify =="
bash "$SETUP_DIR/check_jetson.sh" || true
echo "-- live service status --"
(cd "$EDGE_DIR" && python3 -m tools.status) || echo "  (service status reported an issue — see above)"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================================"
echo "  Upgrade complete."
echo "  UI:     http://${IP:-<jetson-ip>}:8000/ui/setup.html"
echo "  Status:  cd $EDGE_DIR && python3 -m tools.status"
echo "  Logs:   journalctl -u ceravis -f"
echo "============================================================"
