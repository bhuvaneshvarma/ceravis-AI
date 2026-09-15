#!/usr/bin/env bash
# Install + enable the CERAVIS fleet agent (ceravis-fleet-agent.service): the
# small separate service that reports this device to the Fleet Management
# Server and carries out its admins' orders. setup.sh runs this for you; on a
# device set up before the agent existed, run it once after `git pull`.
#
# Nothing per device: the agent reads FMS_URL + FMS_ENROLL_KEY from jetson.env,
# enrolls itself on first start (adopting this device's current edge_id, or
# receiving a new one) and keeps its identity in /var/lib/ceravis-fleet-agent.
#
# Run:  bash setup/install_fleet_agent.sh      (idempotent — safe to re-run)
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
UNIT_SRC="$REPO_DIR/edge/infra/systemd/ceravis-fleet-agent.service"

sed -e "s|/home/ceravis/ceravis2|$REPO_DIR|g" \
    -e "s|^User=.*|User=$USER|" \
    "$UNIT_SRC" | sudo tee /etc/systemd/system/ceravis-fleet-agent.service >/dev/null

# The ONLY things run as root on an admin's order: restarting these two
# services. Must match RESTARTABLE_UNITS in edge/fleet/fms_protocol.
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart ceravis.service, /usr/bin/systemctl restart frpc.service\n' "$USER" \
    | sudo tee /etc/sudoers.d/ceravis-fleet >/dev/null
sudo chmod 0440 /etc/sudoers.d/ceravis-fleet
sudo visudo -cf /etc/sudoers.d/ceravis-fleet >/dev/null \
    || { echo "WARNING: sudoers rule invalid — removing it"; sudo rm -f /etc/sudoers.d/ceravis-fleet; }

# The console's "logs" order reads the journal of ceravis / frpc / the agent.
sudo usermod -a -G systemd-journal "$USER"

sudo systemctl daemon-reload
sudo systemctl enable ceravis-fleet-agent >/dev/null
sudo systemctl restart ceravis-fleet-agent

echo
systemctl status ceravis-fleet-agent --no-pager -n 5 || true
echo
echo "Logs:     journalctl -u ceravis-fleet-agent -f"
echo "Identity: /var/lib/ceravis-fleet-agent/identity.json (written once it has enrolled)"
