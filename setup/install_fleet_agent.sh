#!/usr/bin/env bash
# Install + enable the CERAVIS fleet agent (ceravis-fleet-agent.service): the
# small separate service that reports this device to the Fleet Management
# Server and carries out its admins' orders. setup.sh runs this for you; on a
# device set up before the agent existed, run it once after `git pull`.
#
# Nothing per device: the agent reads FMS_URL from jetson.env and the fleet
# enrollment key (the same for every device, but a SECRET) from the root-only
# /etc/ceravis-fleet-agent/enroll.env this script writes. It enrolls itself on
# first start (adopting this device's current edge_id, or receiving a new one)
# and keeps its identity in /var/lib/ceravis-fleet-agent. From then on it checks
# in with its own key — but keep the enrollment key: enrolling again, after an
# admin's Reset key or into a rebuilt FMS, needs it.
#
# Run:  bash setup/install_fleet_agent.sh      (asks for the key once; idempotent)
#       FMS_ENROLL_KEY=<key> bash setup/install_fleet_agent.sh   (unattended, or to
#       REPLACE a stored key — e.g. after the key is rotated on the FMS)
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
KEY_FILE=/etc/ceravis-fleet-agent/enroll.env

# The enrollment key: given once, kept across re-runs. On disk only root can read
# it; systemd hands it to the agent at start. Never in the repo, which is public.
KEY="${FMS_ENROLL_KEY:-}"
if [ -z "$KEY" ] && ! sudo test -s "$KEY_FILE" && [ -t 0 ]; then
    read -r -s -p "Fleet enrollment key (from the FMS server's fms.env; Enter to skip): " KEY || KEY=""
    echo
fi
if [ -n "$KEY" ]; then
    case "$KEY" in
        *[!A-Za-z0-9._~+/=-]*) echo "refusing: the key has unexpected characters" >&2; exit 2 ;;
    esac
    sudo install -d -m 700 "$(dirname "$KEY_FILE")"
    printf 'FMS_ENROLL_KEY=%s\n' "$KEY" | sudo sh -c "umask 077 && cat > '$KEY_FILE'"
    echo "Enrollment key saved to $KEY_FILE (root only)."
elif sudo test -s "$KEY_FILE"; then
    echo "Keeping the enrollment key in $KEY_FILE (to replace it: FMS_ENROLL_KEY=<key> bash $0)."
else
    echo "NOTE: no enrollment key given. An already-enrolled device keeps checking in, but"
    echo "      cannot re-enroll after a Reset key; a new one idles. Re-run this with the key."
fi

install_unit "$EDGE_DIR/infra/systemd/ceravis-fleet-agent.service"

# The ONLY things run as root on an admin's order: restarting these two
# services. Must match RESTARTABLE_UNITS in edge/fleet/fms_protocol.
install_sudoers ceravis-fleet "$CERAVIS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart ceravis.service, /usr/bin/systemctl restart frpc.service" \
    || echo "WARNING: fleet restart orders will not work on this device"

# The console's "logs" order reads the journal of ceravis / frpc / the agent.
sudo usermod -a -G systemd-journal "$CERAVIS_USER"

sudo systemctl daemon-reload
sudo systemctl enable ceravis-fleet-agent >/dev/null
sudo systemctl restart ceravis-fleet-agent

echo
systemctl status ceravis-fleet-agent --no-pager -n 5 || true
echo
echo "Logs:     journalctl -u ceravis-fleet-agent -f"
echo "Identity: /var/lib/ceravis-fleet-agent/identity.json (written once it has enrolled)"
