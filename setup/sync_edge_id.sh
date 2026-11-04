#!/usr/bin/env bash
# Hand this device's edge_id to the edge app — the same way every other secret
# reaches the app: ONE root-only file,
#
#   /etc/ceravis/credstore/edge_id
#
# which systemd hands ceravis.service as the credential `edge_id` (setup/lib.sh,
# CREDENTIALS). The FMS issues the edge_id when the fleet agent enrolls the
# device; the agent keeps it in its own identity (/var/lib/ceravis-fleet-agent/
# identity.json, beside its key). Only the edge_id is copied — never the key.
#
# Runs as root:
#   • from setup/install_fleet_agent.sh, right after the device enrolled, and
#     from setup/update.sh before it switches release (both with --migrate);
#   • from ceravis-edge-id.path, whenever the agent's identity changes;
#   • at every boot, before ceravis starts (ceravis-edge-id.service).
#
#   --migrate     a home installed before the FMS, not enrolled yet, keeps the
#                 edge_id it routes on today (the app server's, in account.json)
#                 until the FMS confirms it — the agent sends that same value as
#                 legacy_edge_id when it enrolls. Never replaces a stored edge_id.
#   --store-only  write the file only; the caller re-writes the units and
#                 restarts ceravis itself (setup/update.sh).
#
# It also keeps ceravis.service loading exactly what exists: systemd refuses to
# start a unit whose credential file is missing (a sealed image, a file removed
# by hand). Idempotent: nothing changed, nothing restarts.
#
# Run:  sudo bash setup/sync_edge_id.sh [--migrate] [--store-only]
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MIGRATE=0; STORE_ONLY=0
for a in "$@"; do
    case "$a" in
        --migrate) MIGRATE=1 ;;
        --store-only) STORE_ONLY=1 ;;
        *) echo "usage: sudo bash $0 [--migrate] [--store-only]" >&2; exit 2 ;;
    esac
done
IDENTITY="${FMS_IDENTITY_FILE:-/var/lib/ceravis-fleet-agent/identity.json}"
TARGET="$CERAVIS_CREDSTORE/edge_id"
UNIT="${CERAVIS_UNIT_DIR:-/etc/systemd/system}/ceravis.service"

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0" >&2; exit 1; }
# One at a time: the installer's own run and the path unit's never interleave.
exec 9>"${CERAVIS_SYNC_LOCK:-/run/ceravis-edge-id.lock}"
flock 9

# One value from a JSON file, read as data (python -I: nothing from the current
# folder is imported).   json_value <file> <key>
json_value() {
    "$CERAVIS_PYTHON" -I -c '
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    value = data.get(sys.argv[2]) if isinstance(data, dict) else ""
    print(value if isinstance(value, str) else "")
except (OSError, ValueError):
    print("")' "$1" "$2"
}
# The rule the tunnel helper (cloud/apply_edge_id.sh) holds an edge_id to: a
# value it would refuse is never handed to the app either.
routable() { [[ "$1" =~ ^[A-Za-z0-9_-]{3,128}$ ]]; }

EID=""; FROM=""; STATUS=0
if [ -s "$IDENTITY" ]; then
    EID="$(json_value "$IDENTITY" edge_id)"; FROM="the fleet (FMS)"
elif [ "$MIGRATE" = 1 ] && [ ! -s "$TARGET" ] && [ -s "$(data_dir)/account.json" ]; then
    EID="$(json_value "$(data_dir)/account.json" edgeId)"
    FROM="this home's app-server edge_id, kept until the FMS confirms it"
fi

CHANGED=0
if [ -z "$FROM" ]; then
    echo "edge_id: not enrolled with the fleet yet — nothing to hand over"
elif [ -z "$EID" ] && [ ! -s "$IDENTITY" ]; then
    echo "edge_id: not enrolled with the fleet yet, and no earlier edge_id to keep"
elif ! routable "$EID"; then
    echo "edge_id: $FROM gives no usable edge_id ('$EID') — nothing changed" >&2
    STATUS=2
elif [ "$(cat "$TARGET" 2>/dev/null || true)" != "$EID" ]; then
    install -d -m 0700 -o root -g root "$CERAVIS_CREDSTORE"
    # Written aside and moved into place: never a half-written file.
    (umask 077 && printf '%s' "$EID" > "$TARGET.new" && mv -f "$TARGET.new" "$TARGET")
    echo "edge_id: $EID ($FROM) stored in $TARGET (root only)"
    CHANGED=1
fi

if [ "$STORE_ONLY" = 0 ]; then
    if [ -f "$UNIT" ]; then
        loads=0; exists=0
        grep -q '^LoadCredential=edge_id:' "$UNIT" && loads=1
        [ -s "$TARGET" ] && exists=1
        if [ "$loads" != "$exists" ]; then
            install_unit "$EDGE_DIR/infra/systemd/ceravis.service"
            systemctl daemon-reload
            CHANGED=1
        fi
    fi
    # --no-block: at boot this runs ordered BEFORE ceravis, and waiting on a job
    # for it from here would wait forever. try-restart leaves a stopped one be.
    if [ "$CHANGED" = 1 ]; then
        systemctl --no-block try-restart ceravis
        echo "edge_id: ceravis restart requested"
    fi
fi
exit "$STATUS"
