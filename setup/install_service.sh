#!/usr/bin/env bash
# Install + enable the CERAVIS systemd service (auto-start at boot,
# auto-restart on crash) for this layout — see setup/lib.sh.
#
# Run once:  bash setup/install_service.sh
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

install_unit "$EDGE_DIR/infra/systemd/ceravis.service"

sudo systemctl daemon-reload
sudo systemctl enable --now ceravis

echo
systemctl status ceravis --no-pager || true
echo
echo "Logs:    journalctl -u ceravis -f"
echo "Stop:    sudo systemctl stop ceravis"
echo "Restart: sudo systemctl restart ceravis"
