#!/usr/bin/env bash
# Install the nightly reboot timer (03:00-04:00 device-local, randomised) and
# the one sudoers rule the service account needs to reboot.
#
# Run once:  bash setup/install_reboot_timer.sh
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
UNIT_DIR="$REPO_DIR/edge/infra/systemd"
SVC_USER="${SUDO_USER:-$USER}"

for unit in ceravis-reboot.service ceravis-reboot.timer; do
    sed -e "s|/home/ceravis/ceravis2|$REPO_DIR|g" \
        "$UNIT_DIR/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done

# The API runs as the service account, so the MANUAL reboot needs exactly one
# NOPASSWD command — not blanket sudo. visudo -cf validates before install: a
# malformed sudoers file locks everyone out of sudo, so it is never written
# straight into place.
SUDOERS=/etc/sudoers.d/ceravis-reboot
TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: /bin/systemctl reboot\n' "$SVC_USER" > "$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
    sudo install -m 0440 -o root -g root "$TMP" "$SUDOERS"
    echo "sudoers rule installed for '$SVC_USER' ($SUDOERS)"
else
    echo "REFUSED: generated sudoers rule failed validation — not installed." >&2
    rm -f "$TMP"; exit 1
fi
rm -f "$TMP"

sudo systemctl daemon-reload
sudo systemctl enable --now ceravis-reboot.timer

echo
systemctl list-timers ceravis-reboot.timer --no-pager || true
echo
echo "Next run above is the REAL schedule (03:00 + up to 1h random)."
echo
echo "Set the manual-reboot password:  python3 setup/set_reboot_password.py"
echo "Check status:                    curl -s localhost:8000/api/v1/system/reboot"
echo "Dry-run tonight's logic now:     sudo systemctl start ceravis-reboot.service"
echo "                                 (this REALLY reboots if the safety check passes)"
echo "Disable:                         sudo systemctl disable --now ceravis-reboot.timer"
