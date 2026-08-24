#!/usr/bin/env bash
# Grant the CERAVIS service user NetworkManager rights so the hotspot screen
# (Settings -> Hotspot) can create/start/stop the WiFi AP without sudo.
#
# The "hotspot" is not a server: it puts the Jetson's own WiFi radio into
# ACCESS POINT mode via NetworkManager, so the household cameras join the
# Jetson directly instead of the house router. NM also runs DHCP and NAT for
# them (ipv4.method shared), which is why the app needs these rights at all.
#
# Run once:  bash setup/install_hotspot.sh
set -euo pipefail

# SUDO_USER first: run under `sudo`, a bare $USER is root, and the rule would
# then grant root — leaving the UNPRIVILEGED service user (the one that
# actually runs the API) still unable to drive NetworkManager. The hotspot page
# then fails silently, which is the worst way for a permission bug to present.
SVC_USER="${SUDO_USER:-$USER}"
RULE=/etc/polkit-1/rules.d/50-ceravis-network.rules

if [ "$SVC_USER" = "root" ]; then
    echo "REFUSED: could not resolve a non-root service user." >&2
    echo "  Run as the service user:  bash setup/install_hotspot.sh" >&2
    echo "  or pin it:                SUDO_USER=ceravis bash setup/install_hotspot.sh" >&2
    exit 1
fi

sudo tee "$RULE" >/dev/null <<EOF
// Allow the CERAVIS service user ($SVC_USER) to manage NetworkManager
// (needed for the device hotspot the WiFi cameras join).
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "$SVC_USER") {
        return polkit.Result.YES;
    }
});
EOF

sudo systemctl restart polkit 2>/dev/null || true

# Prove the rights actually landed rather than assuming the rule took: a polkit
# rule with the wrong user parses fine and simply never matches.
if command -v nmcli >/dev/null 2>&1; then
    if sudo -u "$SVC_USER" nmcli -t -f DEVICE,TYPE device >/dev/null 2>&1; then
        echo "Polkit rule installed: $RULE"
        echo "  verified: user '$SVC_USER' can query NetworkManager."
    else
        echo "Polkit rule written, but '$SVC_USER' still cannot query" >&2
        echo "  NetworkManager. Check: journalctl -u polkit -n 20" >&2
        exit 1
    fi
    WIFI=$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}')
    if [ -n "${WIFI:-}" ]; then
        echo "  WiFi radio found: $WIFI (the AP will run on this)."
    else
        echo "  NOTE: no WiFi device detected — a hotspot needs one." >&2
    fi
else
    echo "Polkit rule installed: $RULE (nmcli absent — cannot verify here)."
fi

echo "Configure it at: http://<jetson-ip>:8000/ui/hotspot.html"
