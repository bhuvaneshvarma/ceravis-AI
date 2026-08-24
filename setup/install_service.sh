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

# One handle for the whole device. The target uses Wants=, never BindsTo= — see
# the target file for why coupling these lifecycles would be harmful.
sed -e "s|/home/ceravis/ceravis2|$REPO_DIR|g" \
    "$EDGE_DIR/infra/systemd/ceravis.target" \
    | sudo tee /etc/systemd/system/ceravis.target >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now ceravis
sudo systemctl enable ceravis.target

echo
systemctl status ceravis --no-pager || true
echo
echo "Logs:    journalctl -u ceravis -f"
echo "Stop:    sudo systemctl stop ceravis"
echo "Restart: sudo systemctl restart ceravis          (app only)"
echo "         sudo systemctl restart ceravis.target   (app + tunnel + timer)"
