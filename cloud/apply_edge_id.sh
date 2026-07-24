#!/usr/bin/env bash
# Apply an edge_id to the frp tunnel: set the mediamtx-webrtc proxy's `locations`
# to /<edge_id> in frpc.toml and restart frpc.
#
# Installed as /usr/local/bin/ceravis-apply-edge-id by cloud/install_frpc.sh and
# invoked by the edge app (integration/edge_provision) via a NOPASSWD sudoers
# rule for EXACTLY this command when an account is verified. Runs as root.
#
# Safe by design: validates the token (it is spliced into a config + a running
# service), edits ONLY the line carrying the '# ceravis:edge-id' marker (never
# the /api,/ui proxy), verifies the config, and leaves frpc untouched on any
# problem.
set -euo pipefail

EDGE_ID="${1:-}"
FRPC="${FRPC_TOML:-/etc/frp/frpc.toml}"

# Accept only a short safe token — no shell/sed metacharacters reach the config.
if ! [[ "$EDGE_ID" =~ ^[A-Za-z0-9_-]{3,128}$ ]]; then
    echo "refusing: invalid edge_id" >&2; exit 2
fi
[[ -f "$FRPC" ]] || { echo "not found: $FRPC" >&2; exit 3; }
grep -q '# ceravis:edge-id' "$FRPC" || {
    echo "no '# ceravis:edge-id' marker in $FRPC (old template?) — not touching it" >&2
    exit 4; }

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
cp "$FRPC" "$TMP"
# Replace only the `locations = …` on the MARKED line, keeping indentation and
# the marker comment. '|' delimiter so the '#' in the marker is not special.
sed -i -E "/# ceravis:edge-id/s|^([[:space:]]*)locations[[:space:]]*=[^#]*|\1locations = [\"/${EDGE_ID}\"]  |" "$TMP"

# Never restart onto a broken config.
if command -v frpc >/dev/null 2>&1; then
    frpc verify -c "$TMP" >/dev/null 2>&1 || {
        echo "generated config failed 'frpc verify' — aborting" >&2; exit 5; }
fi

install -m 0644 "$TMP" "$FRPC"
systemctl restart frpc
echo "frpc: mediamtx-webrtc locations set to /${EDGE_ID}; restarted"
