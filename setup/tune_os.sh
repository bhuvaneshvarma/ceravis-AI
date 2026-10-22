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
#
# Undo: sudo systemctl set-default graphical.target · sudo apt-mark unhold <pkg>
#
# Run:  sudo bash setup/tune_os.sh           (HEADLESS=0 keeps the desktop)
# The desktop and power-mode changes take effect at the next boot.
set -euo pipefail

POWER_MODE="${POWER_MODE:-25W}"
HEADLESS="${HEADLESS:-1}"

echo "== [1/5] boot target =="
if [ "$HEADLESS" = "1" ]; then
    sudo systemctl set-default multi-user.target >/dev/null
    echo "   boots without the desktop (next boot). A monitor shows a text login."
else
    echo "   desktop kept (HEADLESS=0)"
fi

echo "== [2/5] power mode: $POWER_MODE =="
if command -v nvpmodel >/dev/null 2>&1 && [ -f /etc/nvpmodel.conf ]; then
    ID="$(grep -oE "POWER_MODEL ID=[0-9]+ NAME=$POWER_MODE( |>)" /etc/nvpmodel.conf \
          | grep -oE 'ID=[0-9]+' | cut -d= -f2 | head -1)"
    if [ -n "$ID" ]; then
        # nvpmodel may ask to reboot now; answer "no" — it applies at next boot.
        yes no | sudo nvpmodel -m "$ID" >/dev/null 2>&1 || true
        echo "   mode $ID ($POWER_MODE) set"
    else
        echo "   WARNING: no power mode named $POWER_MODE here. Available:"
        grep -oE 'POWER_MODEL ID=[0-9]+ NAME=[^ >]+' /etc/nvpmodel.conf | sed 's/^/     /'
    fi
else
    echo "   nvpmodel not found — not a Jetson? (skipped)"
fi

echo "== [3/5] fan profile: cool =="
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

echo "== [4/5] hold the NVIDIA stack =="
mapfile -t PKGS < <(dpkg-query -W -f='${Package}\n' \
    'nvidia-l4t-*' 'tensorrt*' 'libnvinfer*' 'libcudnn*' 2>/dev/null | sort -u)
if [ "${#PKGS[@]}" -gt 0 ]; then
    sudo apt-mark hold "${PKGS[@]}" >/dev/null
    echo "   ${#PKGS[@]} packages held (list: apt-mark showhold)"
else
    echo "   no NVIDIA packages found (skipped)"
fi

echo "== [5/5] logs: persistent, capped at 500 MB =="
sudo mkdir -p /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\nSystemMaxUse=500M\n' \
    | sudo tee /etc/systemd/journald.conf.d/ceravis.conf >/dev/null
sudo systemctl restart systemd-journald 2>/dev/null || true
echo "   done"

echo
echo "tune_os: done. Desktop and power-mode changes apply at the next reboot."
