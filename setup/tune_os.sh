#!/usr/bin/env bash
# =============================================================
# Operating-system settings for a CERAVIS appliance. Idempotent.
#
#   1. boot WITHOUT the desktop   the CPU and GPU share 8 GB; the desktop
#                                 holds ~0.5-1 GB for a screen nobody watches
#   2. power mode                 POWER_MODE (default 25W — the bench saw
#                                 over-current warnings above it; set
#                                 POWER_MODE=MAXN_SUPER only after a clean
#                                 load test: no over-current, < 85 °C)
#   3. fan profile "cool"         the fan reacts earlier, the board stays cooler
#   4. hold the NVIDIA packages   an apt upgrade must never replace TensorRT
#                                 (every engine would stop loading) or the
#                                 firmware; Ubuntu security updates still apply
#   5. logs kept, size capped     survive a reboot, never crowd out recordings
#   6. background services off    a desktop image runs printing, Bluetooth,
#                                 snaps, crash upload, modem and firmware
#                                 daemons — CPU, memory and boot time an
#                                 appliance never uses. Masked, not removed.
#   7. OS updates at night        Ubuntu's security updates still install by
#                                 themselves, but in the maintenance window
#                                 (02:00-02:30), just before the nightly reboot
#                                 window, never in the middle of the day
#
# Undo: sudo systemctl set-default graphical.target · sudo apt-mark unhold <pkg>
#       sudo systemctl unmask <unit> && sudo systemctl enable --now <unit>
#       sudo rm -r /etc/systemd/system/apt-daily*.timer.d
#
# Run:  sudo bash setup/tune_os.sh           (HEADLESS=0 keeps the desktop)
# The desktop and power-mode changes take effect at the next boot.
set -euo pipefail

POWER_MODE="${POWER_MODE:-25W}"
HEADLESS="${HEADLESS:-1}"

echo "== [1/7] boot target =="
if [ "$HEADLESS" = "1" ]; then
    sudo systemctl set-default multi-user.target >/dev/null
    echo "   boots without the desktop (next boot). A monitor shows a text login."
else
    echo "   desktop kept (HEADLESS=0)"
fi

echo "== [2/7] power mode: $POWER_MODE =="
if command -v nvpmodel >/dev/null 2>&1 && [ -f /etc/nvpmodel.conf ]; then
    ID="$(grep -oE "POWER_MODEL ID=[0-9]+ NAME=$POWER_MODE( |>)" /etc/nvpmodel.conf \
          | grep -oE 'ID=[0-9]+' | cut -d= -f2 | head -1)"
    if [ -n "$ID" ]; then
        # nvpmodel may ask to reboot now; answer "no" — it applies at next boot.
        yes no 2>/dev/null | sudo nvpmodel -m "$ID" >/dev/null 2>&1 || true
        echo "   mode $ID ($POWER_MODE) set"
    else
        echo "   WARNING: no power mode named $POWER_MODE here. Available:"
        grep -oE 'POWER_MODEL ID=[0-9]+ NAME=[^ >]+' /etc/nvpmodel.conf | sed 's/^/     /'
    fi
else
    echo "   nvpmodel not found — not a Jetson? (skipped)"
fi

echo "== [3/7] fan profile: cool =="
if [ -f /etc/nvfancontrol.conf ]; then
    if grep -q 'FAN_DEFAULT_PROFILE[[:space:]]\+cool' /etc/nvfancontrol.conf; then
        echo "   already cool"
    else
        sudo sed -i 's/^\([[:space:]]*FAN_DEFAULT_PROFILE[[:space:]]\+\)[a-z]\+/\1cool/' /etc/nvfancontrol.conf
        sudo systemctl stop nvfancontrol 2>/dev/null || true
        sudo rm -f /var/lib/nvfancontrol/status
        sudo systemctl start nvfancontrol 2>/dev/null || true
        echo "   set to cool"
    fi
else
    echo "   /etc/nvfancontrol.conf not found (skipped)"
fi

echo "== [4/7] hold the NVIDIA stack =="
# Only what is INSTALLED (status letter 2 = "i": ii, hi): dpkg also knows
# packages that are merely available (libcudnn9, the -cross- ones…), and
# apt-mark refuses to hold those.
mapfile -t PKGS < <(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' \
    'nvidia-l4t-*' 'tensorrt*' 'libnvinfer*' 'libcudnn*' 2>/dev/null \
    | awk 'substr($1, 2, 1) == "i" {print $2}' | sort -u)
if [ "${#PKGS[@]}" -gt 0 ]; then
    sudo apt-mark hold "${PKGS[@]}" >/dev/null \
        || echo "   WARNING: apt-mark could not hold them all (the install goes on)"
    echo "   ${#PKGS[@]} packages held (list: apt-mark showhold)"
else
    echo "   no NVIDIA packages found (skipped)"
fi

echo "== [5/7] logs: persistent, capped at 500 MB =="
sudo mkdir -p /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\nSystemMaxUse=500M\n' \
    | sudo tee /etc/systemd/journald.conf.d/ceravis.conf >/dev/null
sudo systemctl restart systemd-journald 2>/dev/null || true
echo "   done"

echo "== [6/7] background services an appliance does not need: off =="
# Kept on purpose: NetworkManager + wpa_supplicant (the hotspot and the cable),
# avahi (<name>.local), ssh, the USB-C service link (nv-l4t-usb-device-mode),
# zram swap (nvzramconfig), the fan and power daemons, journald, timesyncd.
NOT_NEEDED=(
    cups.service cups.socket cups.path cups-browsed.service     # printing
    bluetooth.service                                           # no Bluetooth devices
    ModemManager.service                                        # no cellular modem
    snapd.service snapd.socket snapd.seeded.service             # no snaps
    whoopsie.service apport.service kerneloops.service          # crash upload to Ubuntu
    packagekit.service                                          # desktop software centre
    fwupd.service fwupd-refresh.timer                           # Jetson firmware comes from NVIDIA's updates
    motd-news.timer ua-timer.timer ubuntu-advantage.service     # Ubuntu news / subscription checks
    nvargus-daemon.service                                      # CSI cameras (ours are IP cameras)
    docker.service docker.socket containerd.service             # no containers
)
OFF=()
for u in "${NOT_NEEDED[@]}"; do
    systemctl list-unit-files --no-legend "$u" 2>/dev/null | grep -q . || continue
    sudo systemctl disable --now "$u" >/dev/null 2>&1 || true
    sudo systemctl mask "$u" >/dev/null 2>&1 && OFF+=("${u%.*}")
done
echo "   off: ${OFF[*]:-nothing to turn off}"

echo "== [7/7] OS security updates in the night window =="
if systemctl list-unit-files --no-legend apt-daily-upgrade.timer 2>/dev/null | grep -q .; then
    for t in apt-daily:01:30 apt-daily-upgrade:02:00; do
        sudo mkdir -p "/etc/systemd/system/${t%%:*}.timer.d"
        # Persistent=false: a device that was off at night does not catch up by day.
        printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* %s\nRandomizedDelaySec=30m\nPersistent=false\n' \
            "${t#*:}" | sudo tee "/etc/systemd/system/${t%%:*}.timer.d/ceravis.conf" >/dev/null
    done
    sudo systemctl daemon-reload
    echo "   download 01:30, install 02:00 (+ up to 30 min); the reboot window follows"
else
    echo "   no apt timers (skipped)"
fi

echo
echo "tune_os: done. Desktop and power-mode changes apply at the next reboot."
