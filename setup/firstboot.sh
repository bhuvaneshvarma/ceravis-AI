#!/usr/bin/env bash
# =============================================================
# CERAVIS first boot — gives THIS device its own identity.
#
# Runs once, as root, from ceravis-firstboot.service, before the CERAVIS
# services start: on the master right after install, and on every device
# copied from a sealed master image (setup/seal.sh removes the marker).
#
#   1. machine-id          a new one if the image carried none
#   2. SSH host keys       new keys — copies never share a host fingerprint
#   3. hostname            ceravis-<code>, from the Jetson serial (unique)
#   4. TLS certificate     a new one for this name (LAN HTTPS for MediaMTX)
#   5. tunnel prefix       frp proxy names prefixed with ceravis-<code>
#   6. camera hotspot      CERAVIS-<CODE> with a new random password
#                          (shown on the device's first-run screen)
#
# Each step is best-effort and logged; one failing never stops the others.
# Re-run by hand:  sudo rm /etc/ceravis/.firstboot-done && sudo bash setup/firstboot.sh
# =============================================================
set -uo pipefail

LAYOUT=/etc/ceravis/layout.env
# shellcheck disable=SC1090
[ -f "$LAYOUT" ] && . "$LAYOUT"
# shellcheck source=setup/lib.sh
. "${CERAVIS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/setup/lib.sh"
export_layout
MARKER=/etc/ceravis/.firstboot-done

say() { echo "firstboot: $*"; }

NAME="$(device_name 2>/dev/null || true)"
[ -n "$NAME" ] || say "could not work out the device name — identity steps skipped"

# 1. machine-id ------------------------------------------------------------
if [ ! -s /etc/machine-id ]; then
    systemd-machine-id-setup >/dev/null 2>&1 && say "new machine-id"
fi

# 2. SSH host keys ----------------------------------------------------------
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    ssh-keygen -A >/dev/null 2>&1 && say "new SSH host keys"
fi

# 3. hostname -----------------------------------------------------------------
if [ -n "$NAME" ] && [ "$(hostname)" != "$NAME" ]; then
    hostnamectl set-hostname "$NAME" 2>/dev/null || echo "$NAME" > /etc/hostname
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NAME/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$NAME" >> /etc/hosts
    fi
    say "hostname $NAME"
fi

# 4. TLS certificate (made only when missing — the seal removes it) ---------
bash "$CERAVIS_ROOT/setup/install_mediamtx.sh" >/dev/null 2>&1 \
    && say "media server certificate ready" || say "certificate step failed (LAN HTTPS off)"

# 5. tunnel prefix ----------------------------------------------------------
if [ -n "$NAME" ] && [ -f /etc/frp/frpc.toml ]; then
    frpc_set_user /etc/frp/frpc.toml "$NAME" && say "tunnel prefix $NAME"
fi

# 6. camera hotspot ----------------------------------------------------------
DATA="$(data_dir)"
mkdir -p "$DATA"
if (cd "$EDGE_DIR" && "$CERAVIS_PYTHON" -m common.hotspot ensure) >/dev/null 2>&1; then
    say "camera hotspot ready"
else
    say "hotspot not started (no Wi-Fi adapter, or NetworkManager not ready) — set it up from the Hotspot page"
fi
chown -R "$CERAVIS_USER" "$DATA" 2>/dev/null || true

# A monitor, if one is ever plugged in, shows which device this is and where
# to open it (\4 = its IPv4 address, filled in by the login prompt).
if [ -n "$NAME" ]; then
    printf 'CERAVIS edge device %s\nOpen  http://\\4:8000  in a browser on the same network.\n\n' "$NAME" > /etc/issue
fi

mkdir -p "$(dirname "$MARKER")"
date -Is > "$MARKER"
say "done"
