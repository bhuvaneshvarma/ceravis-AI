#!/usr/bin/env bash
# Grant the CERAVIS service user NetworkManager rights so the hotspot screen
# (Settings -> Hotspot) can create/start/stop the WiFi AP without sudo.
#
# Run once:  bash setup/install_hotspot.sh
set -euo pipefail

RULE=/etc/polkit-1/rules.d/50-ceravis-network.rules

sudo tee "$RULE" >/dev/null <<EOF
// Allow the CERAVIS service user ($USER) to manage NetworkManager
// (needed for the device hotspot the WiFi cameras join).
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "$USER") {
        return polkit.Result.YES;
    }
});
EOF

sudo systemctl restart polkit 2>/dev/null || true
echo "Polkit rule installed: $RULE (user $USER can manage the hotspot)."
echo "Configure it at: http://<jetson-ip>:8000/ui/hotspot.html"
