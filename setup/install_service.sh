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

# jetson.env is gitignored and generated, so a fresh clone has none. Create it
# before the unit exists, so there is no window where the service is installed
# and enabled but has nothing to read.
ENV_DIR="$EDGE_DIR/infra/env"
if [ ! -f "$ENV_DIR/jetson.env" ]; then
    cp "$ENV_DIR/jetson.env.example" "$ENV_DIR/jetson.env"
    echo "created $ENV_DIR/jetson.env from the template"
fi

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
