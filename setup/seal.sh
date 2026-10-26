#!/usr/bin/env bash
# =============================================================
# CERAVIS seal — prepare a MASTER device to be copied (golden image).
#
# Removes everything that belongs to THIS device or a customer, so every copy
# starts fresh and gives itself its own identity at first boot (firstboot.sh):
#
#   customer   account (edge_id, care recipient), sign-in sessions, cameras,
#              zones, enrollment photos and galleries, events, recordings,
#              upload queue, talk-back credentials, reboot password
#   device     machine-id, SSH host keys, TLS certificate, hotspot, saved
#              Wi-Fi networks, fleet identity, tunnel routes and prefix
#   traces     logs, shell histories, temporary files, DHCP leases
#
# Kept: the installed software, the AI engines, the cloud server settings.
# The admin account's password is EXPIRED: the first person to log in to a copy
# must set a new one, so no two devices share it.
#
# This cannot be undone. The cloud's record of a care account that was signed
# in on this device is NOT removed — remove it there if needed.
#
# Run:  sudo bash setup/seal.sh            (asks you to type SEAL)
#       sudo bash setup/seal.sh --yes --poweroff
# =============================================================
set -uo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"     # reads /etc/ceravis/layout.env

YES=0; POWEROFF=0
for a in "$@"; do
    case "$a" in
        --yes) YES=1 ;;
        --poweroff) POWEROFF=1 ;;
        *) echo "usage: sudo bash setup/seal.sh [--yes] [--poweroff]" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash setup/seal.sh" >&2; exit 1; }

DATA="$(data_dir)"
echo "This SEALS $(hostname) as a master image: every customer and device-specific"
echo "file in $DATA and the system identity are deleted. It cannot be undone."
if [ "$YES" != 1 ]; then
    read -r -p "Type SEAL to continue: " ANSWER
    [ "$ANSWER" = "SEAL" ] || { echo "not sealed"; exit 1; }
fi

say() { echo "seal: $*"; }

say "stopping the CERAVIS services"
systemctl stop ceravis frpc ceravis-fleet-agent ceravis-reboot.timer 2>/dev/null || true

say "customer and runtime data"
for f in account.json ui_sessions.json hotspot.json cameras.json zones.json \
         reboot_auth.json last_reboot.json recording_mode.json \
         talkback.json talkback_guard.json talkback_log.jsonl talkback_log.jsonl.1 \
         cloud_calls.jsonl ceravis_api_wire.jsonl mediamtx.yml mediamtx.log; do
    rm -f "$DATA/$f"
done
rm -f "$DATA"/ceravis.db "$DATA"/ceravis.db-*
for d in events recordings recordings_proof outbox recipients certs .cache; do
    rm -rf "${DATA:?}/$d"
done

say "fleet identity"
rm -rf /var/lib/ceravis-fleet-agent/* 2>/dev/null || true

if [ -f /etc/frp/frpc.toml ]; then
    say "tunnel routes and prefix back to their placeholders"
    frpc_reset_routes /etc/frp/frpc.toml
fi

say "hotspot and saved Wi-Fi networks (wired connections kept)"
if command -v nmcli >/dev/null 2>&1; then
    nmcli -t -f NAME,TYPE connection show 2>/dev/null \
        | awk -F: '$2=="802-11-wireless" {print $1}' \
        | while IFS= read -r con; do nmcli connection delete "$con" >/dev/null 2>&1 || true; done
fi
rm -f /var/lib/NetworkManager/*.lease /var/lib/NetworkManager/dnsmasq-*.leases 2>/dev/null || true

say "SSH host keys and machine-id (new ones at first boot)"
rm -f /etc/ssh/ssh_host_*
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id && ln -s /etc/machine-id /var/lib/dbus/machine-id

say "admin passwords expire — each copy's first login sets its own"
awk -F: '$3 >= 1000 && $3 < 60000 && $7 !~ /(nologin|false)$/ {print $1}' /etc/passwd \
    | while IFS= read -r u; do chage -d 0 "$u" && say "  $u: new password required at first login"; done

say "logs, histories and temporary files"
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
find /var/log -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete 2>/dev/null || true
find /var/log -type f -name '*.log' -exec truncate -s 0 {} + 2>/dev/null || true
rm -f /root/.bash_history /home/*/.bash_history
rm -rf /tmp/* /var/tmp/* 2>/dev/null || true

say "first boot will run on the next start"
rm -f /etc/ceravis/.firstboot-done
systemctl enable ceravis-firstboot >/dev/null 2>&1 || true
sync

echo
echo "Sealed. Power off and capture the disk now (Clonezilla / NVIDIA backup)."
echo "Do NOT boot this disk again before capturing it — booting it unseals it."
[ "$POWEROFF" = 1 ] && systemctl poweroff
exit 0
