#!/usr/bin/env bash
# Hand this device's edge_id from the fleet agent to the edge app — the same way
# every other secret reaches the app: ONE root-only file,
#
#   /etc/ceravis/credstore/edge_id
#
# which systemd hands ceravis.service as the credential `edge_id` (setup/lib.sh,
# CREDENTIALS). The FMS issues the edge_id when the agent enrolls the device;
# the agent keeps it in its own identity (/var/lib/ceravis-fleet-agent/
# identity.json, beside its key). Only the edge_id is copied — never the key.
#
# Runs as root:
#   • from setup/install_fleet_agent.sh, right after the device enrolled;
#   • from ceravis-edge-id.path, whenever the agent's identity changes (an
#     enrollment after the install — e.g. one made offline — or a new edge_id);
#   • at every boot, before ceravis starts (ceravis-edge-id.service).
# Idempotent: when the file already holds this edge_id and the unit loads it,
# nothing changes and nothing restarts. Not enrolled yet: nothing to do.
#
# Run:  sudo bash setup/sync_edge_id.sh
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

IDENTITY="${FMS_IDENTITY_FILE:-/var/lib/ceravis-fleet-agent/identity.json}"
TARGET="$CERAVIS_CREDSTORE/edge_id"
UNIT="${CERAVIS_UNIT_DIR:-/etc/systemd/system}/ceravis.service"

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0" >&2; exit 1; }
if [ ! -s "$IDENTITY" ]; then
    echo "edge_id: not enrolled with the fleet yet — nothing to hand over"
    exit 0
fi

# Read as data (python -I: nothing from the current folder is imported).
EID="$("$CERAVIS_PYTHON" -I -c '
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("edge_id") or "") if isinstance(data, dict) else "")
except (OSError, ValueError):
    print("")' "$IDENTITY")"
# The same rule the tunnel helper (cloud/apply_edge_id.sh) holds an edge_id to:
# a value it would refuse is never handed to the app either.
if ! [[ "$EID" =~ ^[A-Za-z0-9_-]{3,128}$ ]]; then
    echo "edge_id: the fleet identity holds no usable edge_id ('$EID') — nothing changed" >&2
    exit 2
fi

CHANGED=0
if [ "$(cat "$TARGET" 2>/dev/null || true)" != "$EID" ]; then
    install -d -m 0700 -o root -g root "$CERAVIS_CREDSTORE"
    # Written aside and moved into place: never a half-written file.
    (umask 077 && printf '%s' "$EID" > "$TARGET.new" && mv -f "$TARGET.new" "$TARGET")
    echo "edge_id: $EID stored in $TARGET (root only)"
    CHANGED=1
fi
# The unit loads a credential only if its file existed when the unit was written.
if [ -f "$UNIT" ] && ! grep -q '^LoadCredential=edge_id:' "$UNIT"; then
    install_unit "$EDGE_DIR/infra/systemd/ceravis.service"
    systemctl daemon-reload
    CHANGED=1
fi
# --no-block: at boot this runs ordered BEFORE ceravis, and waiting on a job for
# it from here would wait forever. A stopped service just starts with it later.
if [ "$CHANGED" = 1 ] && systemctl is-active --quiet ceravis; then
    systemctl --no-block try-restart ceravis
    echo "edge_id: ceravis restarting on $EID"
fi
