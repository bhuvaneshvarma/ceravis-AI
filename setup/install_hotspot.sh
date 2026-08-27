#!/usr/bin/env bash
# Grant the CERAVIS service user NetworkManager rights so the hotspot screen
# (Settings -> Hotspot) can create/start/stop the WiFi AP without sudo.
#
# The "hotspot" is not a server: it puts the Jetson's own WiFi radio into
# ACCESS POINT mode via NetworkManager, so the household cameras join the
# Jetson directly instead of the house router. NM also runs DHCP and NAT for
# them (ipv4.method shared), which is why the app needs these rights at all.
#
# polkit ships in two mutually exclusive flavours and this script targets
# whichever is actually present:
#   * >= 0.106 (Ubuntu 22.10+ / newer JetPack): JavaScript rules in
#       /etc/polkit-1/rules.d/*.rules
#   * <= 0.105 (Ubuntu 22.04 / JetPack 6 default): the older .pkla format in
#       /etc/polkit-1/localauthority/50-local.d/*.pkla
# An 0.105 polkit SILENTLY IGNORES a .rules file, so writing the wrong one is
# the worst kind of bug — it looks installed and never grants anything. This
# script writes the right one, deletes the other so a re-run never leaves a
# stale grant behind, and then proves the grant is live by asking NM directly.
#
# Run once:  bash setup/install_hotspot.sh
set -euo pipefail

# SUDO_USER first: run under `sudo`, a bare $USER is root, and the rule would
# then grant root — leaving the UNPRIVILEGED service user (the one that
# actually runs the API) still unable to drive NetworkManager. The hotspot page
# then fails silently, which is the worst way for a permission bug to present.
SVC_USER="${SUDO_USER:-$USER}"
JS_RULE=/etc/polkit-1/rules.d/50-ceravis-network.rules
PKLA_RULE=/etc/polkit-1/localauthority/50-local.d/50-ceravis-network.pkla

if [ "$SVC_USER" = "root" ]; then
    echo "REFUSED: could not resolve a non-root service user." >&2
    echo "  Run as the service user:  bash setup/install_hotspot.sh" >&2
    echo "  or pin it:                SUDO_USER=ceravis bash setup/install_hotspot.sh" >&2
    exit 1
fi

# Which polkit backend is on this box? Versions <= 0.105 have no JavaScript
# engine and silently ignore rules.d, so those must use a .pkla file instead.
# When the version can't be read, the older .pkla is the safe default: it is
# also honoured by newer polkit, whereas a .rules file on old polkit is not.
PK_VER="$(pkaction --version 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)?' | head -1 || true)"
use_pkla=0
case "$PK_VER" in
    0.0*|0.10[0-5]) use_pkla=1 ;;   # 0.100 .. 0.105 -> legacy pkla
    "")             use_pkla=1 ;;   # unknown        -> pkla is the safe default
esac

if [ "$use_pkla" -eq 1 ]; then
    sudo rm -f "$JS_RULE"                       # never leave the other backend behind
    sudo mkdir -p "$(dirname "$PKLA_RULE")"
    sudo tee "$PKLA_RULE" >/dev/null <<EOF
[Let the CERAVIS service user ($SVC_USER) manage NetworkManager]
Identity=unix-user:$SVC_USER
Action=org.freedesktop.NetworkManager.*
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF
    RULE="$PKLA_RULE"
    echo "polkit ${PK_VER:-unknown}: using legacy .pkla backend"
else
    sudo rm -f "$PKLA_RULE"                     # never leave the other backend behind
    sudo mkdir -p "$(dirname "$JS_RULE")"
    sudo tee "$JS_RULE" >/dev/null <<EOF
// Allow the CERAVIS service user ($SVC_USER) to manage NetworkManager
// (needed for the device hotspot the WiFi cameras join).
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "$SVC_USER") {
        return polkit.Result.YES;
    }
});
EOF
    RULE="$JS_RULE"
    echo "polkit $PK_VER: using JavaScript rules.d backend"
fi

sudo systemctl restart polkit 2>/dev/null || true

# Prove the grant actually took. `nmcli general permissions` is the real test:
# it asks NetworkManager what THIS user is authorised to do, so a mis-targeted
# rule (wrong user, wrong backend) shows up as a plain "no"/"auth" here rather
# than "yes". A polkit file with the wrong user parses fine and never matches.
if command -v nmcli >/dev/null 2>&1; then
    PERMS="$(sudo -u "$SVC_USER" nmcli -t -f PERMISSION,VALUE general permissions 2>/dev/null || true)"
    if echo "$PERMS" | grep -q "settings.modify.system:yes"; then
        echo "Polkit rule installed: $RULE"
        echo "  verified: '$SVC_USER' may create/modify NetworkManager connections."
    else
        echo "Polkit rule written to $RULE, but '$SVC_USER' is still NOT" >&2
        echo "  authorised (settings.modify.system is not 'yes')." >&2
        echo "  polkit version seen: ${PK_VER:-unknown}" >&2
        echo "  Check:  journalctl -u polkit -n 20" >&2
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
