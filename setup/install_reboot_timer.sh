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

# RTC-less board hygiene. Without a battery-backed clock the Jetson boots at
# 1970 until NTP corrects it, and a wall-clock timer then fires at the wrong
# instant. Two enablements make the nightly timer safe on such hardware:
#   1) systemd-timesyncd disciplines the clock over the network AND, on shutdown,
#      saves the time to /var/lib/systemd/timesync/clock which it bumps the clock
#      forward to on the next boot — so a cold boot lands near the last-known time
#      instead of 1970.
#   2) systemd-time-wait-sync makes time-sync.target actually MEAN "clock is
#      synchronised" (without it the target is reached almost immediately and
#      guarantees nothing), so the reboot service's After=time-sync.target holds.
# Both are best-effort: a stripped image may lack the units, and scheduled_reboot.py
# still self-guards on the sync flag + the window regardless.
sudo systemctl enable --now systemd-timesyncd 2>/dev/null \
    && echo "systemd-timesyncd enabled (network time + persistent clock)" \
    || echo "note: systemd-timesyncd not available — ensure SOME NTP client runs"
sudo systemctl enable systemd-time-wait-sync 2>/dev/null \
    && echo "systemd-time-wait-sync enabled (time-sync.target now means synced)" \
    || echo "note: systemd-time-wait-sync not available — the in-script clock guard still applies"

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
