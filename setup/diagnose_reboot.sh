#!/usr/bin/env bash
# One-shot nightly-reboot diagnostic. Reboots NOTHING — it only reports. Run it
# on the device and paste the whole output back:
#
#   bash setup/diagnose_reboot.sh
#
# It answers, in order, the only questions that matter about "why didn't the
# nightly reboot happen":
#   1. Is the device even reading the correct time right now? (RTC-less boards
#      boot at 1970 — a wall-clock timer is meaningless until NTP corrects it.)
#   2. How long has it been up? (A device powered off overnight is simply never
#      running at 03:00, so the timer never fires — that is not a bug.)
#   3. Is the fix actually deployed here? (git HEAD)
#   4. Is the timer installed, enabled and scheduled? (systemd's REAL view)
#   5. What did every PAST scheduled run actually decide? (the service journal —
#      this is the definitive record, not `last reboot`, which only shows power
#      cycles and cannot show a clean software reboot as anything but a gap.)
#   6. Is the privileged reboot permitted? (the NOPASSWD sudoers rule)
#   7. What would tonight's run decide RIGHT NOW? (scheduled_reboot.py --explain)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$HERE")"
EDGE_DIR="$REPO_DIR/edge"
SVC_USER="${SUDO_USER:-$USER}"

line() { printf '\n========== %s ==========\n' "$1"; }

line "1. CLOCK — is the device reading the correct time?"
date
command -v timedatectl >/dev/null 2>&1 && timedatectl || echo "(no timedatectl)"

line "2. UPTIME — was it even running at 03:00?"
command -v uptime >/dev/null 2>&1 && uptime -p 2>/dev/null || true
[ -r /proc/uptime ] && awk '{printf "up %.0f seconds (%.1f hours)\n",$1,$1/3600}' /proc/uptime

line "3. DEPLOYED CODE — is the fix on this device?"
git -C "$REPO_DIR" log --oneline -3 2>/dev/null || echo "(not a git checkout)"
git -C "$REPO_DIR" status --short 2>/dev/null | head -5 || true

line "4. TIMER — installed, enabled, scheduled?"
systemctl is-enabled ceravis-reboot.timer 2>&1 || true
systemctl is-active  ceravis-reboot.timer 2>&1 || true
systemctl list-timers ceravis-reboot.timer --all --no-pager 2>&1 || true

line "5. SERVICE JOURNAL — what every PAST scheduled run actually did"
echo "(the real record — 'last reboot' only shows power cycles, never a clean reboot)"
journalctl -u ceravis-reboot.service --no-pager -n 100 2>&1 \
    || echo "(no journal access — try with sudo)"

line "6. PERMISSION — is the privileged reboot allowed without a password?"
if sudo -n -l /bin/systemctl reboot >/dev/null 2>&1; then
    echo "OK: '$SVC_USER' may run /bin/systemctl reboot with NOPASSWD"
else
    echo "MISSING: the NOPASSWD sudoers rule for /bin/systemctl reboot is not in"
    echo "         place for '$SVC_USER' — a scheduled reboot would be REFUSED."
    echo "         Fix: bash setup/install_reboot_timer.sh"
fi

line "7. DECISION NOW — what would tonight's run do at this moment?"
if command -v python3 >/dev/null 2>&1; then
    ( cd "$EDGE_DIR" && python3 tools/scheduled_reboot.py --explain 2>&1 ) \
        | grep -vE "SQLite store" || true
else
    echo "(no python3 on PATH)"
fi

line "DONE"
echo "Paste everything above. To PROVE the reboot executes end-to-end at any"
echo "hour (this REALLY reboots — only when you are ready):"
echo "    sudo systemctl start ceravis-reboot.service     # exercises the exact"
echo "                                                    # production path in-window"
echo "    sudo -E python3 $EDGE_DIR/tools/scheduled_reboot.py --force  # any hour"
