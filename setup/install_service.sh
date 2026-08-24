#!/usr/bin/env bash
# Install + enable the CERAVIS systemd service (auto-start at boot,
# auto-restart on crash). Substitutes this clone's path and your user.
#
# Run once:  bash setup/install_service.sh
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
EDGE_DIR="$REPO_DIR/edge"
UNIT_SRC="$EDGE_DIR/infra/systemd/ceravis.service"

sed -e "s|/home/ceravis/ceravis2|$REPO_DIR|g" \
    -e "s|^User=.*|User=$USER|" \
    "$UNIT_SRC" | sudo tee /etc/systemd/system/ceravis.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now ceravis

echo
systemctl status ceravis --no-pager || true
echo
echo "Logs:    journalctl -u ceravis -f"
echo "Stop:    sudo systemctl stop ceravis"
echo "Restart: sudo systemctl restart ceravis"
