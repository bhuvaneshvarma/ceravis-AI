#!/usr/bin/env bash
# =============================================================
# CERAVIS one-shot upgrade — OSNet ReID, FastReid cleanup, scipy fix
# =============================================================
# Pulls latest code, purges every FastReid leftover + stale caches,
# repairs dependencies (scipy/matplotlib via apt), builds the OSNet ReID
# engine, and restarts the service. Idempotent — safe to re-run.
#
# Run:  bash scripts/upgrade.sh
set -euo pipefail

EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(dirname "$EDGE_DIR")"
cd "$EDGE_DIR"

echo "== [1/6] latest code =="
git -C "$REPO_DIR" pull --ff-only || echo "  (pull skipped — resolve manually if needed)"

echo "== [2/6] purge FastReid leftovers + stale caches =="
rm -f models/reid/fastreid* models/reid/*fast* 2>/dev/null || true
rm -rf /tmp/ceravis-export-venv          # rebuilt clean for OSNet below
pip3 cache purge 2>/dev/null || true
find "$EDGE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
sudo apt-get autoremove -y >/dev/null 2>&1 || true
echo "  cleaned."

echo "== [3/6] dependencies (scipy/matplotlib via apt fixes 'No module scipy') =="
bash scripts/install_native.sh

echo "== [4/6] build OSNet ReID engine =="
bash scripts/export_reid.sh

echo "== [5/6] restart service =="
sudo systemctl restart ceravis
sleep 6

echo "== [6/6] verify =="
bash scripts/check_jetson.sh || true
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================================"
echo "  Upgrade complete."
echo "  UI:     http://${IP:-<jetson-ip>}:8000/ui/setup.html"
echo "  Detect test:  python3 scripts/test_detect.py"
echo "  Logs:   journalctl -u ceravis -f"
echo "============================================================"
