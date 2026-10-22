#!/usr/bin/env bash
# Install the CERAVIS app-server API key: the X-API-Key header on EVERY call the
# edge makes to the app server (sign-in, cameras, alerts, snapshots, status).
# It is the same for every device, but a SECRET — and this repository is public
# — so it is never in the repo. It lives in a root-only file this script writes:
#
#   development  /etc/ceravis/secrets.env (CERAVIS_API_KEY=…), which systemd
#                reads as root into the app's environment
#   production   /etc/ceravis/credstore/ceravis_api_key (the key alone), which
#                systemd hands the app as a credential — never an environment
#                variable (setup/lib.sh, CERAVIS_CREDSTORE)
#
# setup.sh / install.sh / upgrade.sh run this for you; the key takes effect when
# ceravis restarts, which they also do.
#
# Run:  bash setup/install_api_key.sh      (asks for the key once; idempotent)
#       CERAVIS_API_KEY=<key> bash setup/install_api_key.sh   (unattended, or to
#       REPLACE a stored key — e.g. after the key is rotated on the app server)
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if [ -n "$CERAVIS_CREDSTORE" ]; then
    KEY_FILE="$CERAVIS_CREDSTORE/ceravis_api_key"; FORMAT='%s'
else
    KEY_FILE=/etc/ceravis/secrets.env; FORMAT='CERAVIS_API_KEY=%s\n'
fi

# The key: given once, kept across re-runs. On disk only root can read it.
KEY="${CERAVIS_API_KEY:-}"
if [ -z "$KEY" ] && ! sudo test -s "$KEY_FILE" && [ -t 0 ]; then
    read -r -s -p "CERAVIS app-server API key (Enter to skip): " KEY || KEY=""
    echo
fi
if [ -n "$KEY" ]; then
    case "$KEY" in
        *[!A-Za-z0-9._~+/=-]*) echo "refusing: the key has unexpected characters" >&2; exit 2 ;;
    esac
    sudo install -d -m 700 -o root -g root "$(dirname "$KEY_FILE")"
    # Written aside and moved into place: a fresh 600 file every time, never a
    # half-written one, whatever mode an older copy had.
    # shellcheck disable=SC2059
    printf "$FORMAT" "$KEY" \
        | sudo sh -c "umask 077 && cat > '$KEY_FILE.new' && mv -f '$KEY_FILE.new' '$KEY_FILE'"
    echo "API key saved to $KEY_FILE (root only)."
elif sudo test -s "$KEY_FILE"; then
    echo "Keeping the API key in $KEY_FILE (to replace it: CERAVIS_API_KEY=<key> bash $0)."
else
    echo "NOTE: no API key given, and /api/v1/system/status will say so. Without a valid"
    echo "      key the app server refuses this device's calls (alerts stay queued, not"
    echo "      lost). Re-run this with the key."
fi
