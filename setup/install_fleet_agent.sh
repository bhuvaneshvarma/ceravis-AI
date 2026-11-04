#!/usr/bin/env bash
# Install + enable the CERAVIS fleet agent (ceravis-fleet-agent.service): the
# small separate service that reports this device to the Fleet Management
# Server and carries out its admins' orders. setup.sh runs this for you; on a
# device set up before the agent existed, run it once after `git pull`.
#
# Nothing per device: the agent reads FMS_URL from jetson.env and the fleet
# enrollment key (the same for every device, but a SECRET) from the root-only
# /etc/ceravis-fleet-agent/enroll.env this script writes. It ENROLLS the device
# as soon as it starts — this script waits for that — and the FMS issues the
# device's edge_id (the only place edge_ids come from). The agent keeps its
# identity in /var/lib/ceravis-fleet-agent; the edge_id alone is handed to the
# app as a root-only credential (setup/sync_edge_id.sh, now and on every later
# change). From then on it checks in with its own key — but keep the enrollment
# key: enrolling again, after an admin's Reset key or into a rebuilt FMS, needs it.
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
# The edge_id hand-over (setup/sync_edge_id.sh): at every boot before ceravis,
# and whenever the agent's identity changes.
install_unit "$EDGE_DIR/infra/systemd/ceravis-edge-id.service"
install_unit "$EDGE_DIR/infra/systemd/ceravis-edge-id.path"

# The ONLY things run as root on an admin's order: restarting these two
# services. Must match RESTARTABLE_UNITS in edge/fleet/fms_protocol.
install_sudoers ceravis-fleet "$CERAVIS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart ceravis.service, /usr/bin/systemctl restart frpc.service" \
    || echo "WARNING: fleet restart orders will not work on this device"

# The console's "logs" order reads the journal of ceravis / frpc / the agent.
sudo usermod -a -G systemd-journal "$CERAVIS_USER"

sudo systemctl daemon-reload
sudo systemctl enable ceravis-fleet-agent ceravis-edge-id.service >/dev/null
sudo systemctl enable --now ceravis-edge-id.path >/dev/null
sudo systemctl restart ceravis-fleet-agent

# Enroll now, while the installer is here to see it: the agent enrolls the moment
# it starts. Wait for its identity, then hand the edge_id to the app.
IDENTITY=/var/lib/ceravis-fleet-agent/identity.json
FMS_URL="$(sed -n 's/^[[:space:]]*FMS_URL[[:space:]]*=[[:space:]]*//p' "$EDGE_DIR/infra/env/jetson.env" \
           | tail -1 | tr -d '\r"'"'"' ')"
if [ -n "$FMS_URL" ] && sudo test -s "$KEY_FILE" && ! sudo test -s "$IDENTITY"; then
    echo "Enrolling this device with the fleet ($FMS_URL)..."
    for _ in $(seq 1 "${ENROLL_WAIT_SECS:-90}"); do
        sudo test -s "$IDENTITY" && break
        sleep 1
    done
fi
echo
if sudo test -s "$IDENTITY"; then
    sudo bash "$CERAVIS_ROOT/setup/sync_edge_id.sh"
else
    echo "NOTE: not enrolled with the fleet yet. The agent keeps trying by itself, and the"
    echo "      edge_id reaches the app the moment the FMS issues it. Its latest words:"
    sudo journalctl -u ceravis-fleet-agent -n 3 --no-pager -o cat 2>/dev/null | sed 's/^/        /' || true
fi

echo
systemctl status ceravis-fleet-agent --no-pager -n 5 || true
echo
echo "Logs:     journalctl -u ceravis-fleet-agent -f"
echo "Identity: /var/lib/ceravis-fleet-agent/identity.json (written once it has enrolled)"
echo "edge_id:  sudo cat $CERAVIS_CREDSTORE/edge_id"
