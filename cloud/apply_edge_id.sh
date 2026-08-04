#!/usr/bin/env bash
# Apply this device's edge_id to the frp tunnel: set the mediamtx-webrtc proxy's
# `locations` to ["/<edge_id>"] in frpc.toml and restart frpc.
#
# Installed as /usr/local/bin/ceravis-apply-edge-id by cloud/install_frpc.sh and
# invoked by the edge app (integration/edge_provision) via a NOPASSWD sudoers
# rule for EXACTLY this command — at account verify AND on every service boot
# (main._apply_edge_id_on_boot). So a fresh, redeployed, or hand-edited config
# self-heals to the right value on the next start, with no manual step. Runs as root.
#
# Robust by design — the whole point is that the live links can never silently
# break on a config edit again:
#   • Targets the proxy by NAME (`name = "mediamtx-webrtc"`), NOT a comment
#     marker, so an edit that drops the marker can't defeat it.
#   • ALWAYS writes the leading slash: ["/<edge_id>"]. frp matches a request PATH
#     (always starts with "/") against this string — a missing slash silently
#     404s every live link (the exact bug this guards against).
#   • Inserts the `locations` line if the block has none.
#   • Idempotent: if the value is already correct it makes NO change and does NOT
#     restart frpc, so the boot-time self-heal is free when nothing moved.
#   • Validates the token, runs `frpc verify`, and leaves frpc untouched on any
#     problem — never restarts onto a broken config.
set -euo pipefail

EDGE_ID="${1:-}"
FRPC="${FRPC_TOML:-/etc/frp/frpc.toml}"

# Accept only a short safe token — no shell/sed metacharacters, and NO slash:
# the slash is added here, exactly once, so the stored value is always "/<id>".
if ! [[ "$EDGE_ID" =~ ^[A-Za-z0-9_-]{3,128}$ ]]; then
    echo "refusing: invalid edge_id" >&2; exit 2
fi
[[ -f "$FRPC" ]] || { echo "not found: $FRPC" >&2; exit 3; }

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT

# Rewrite ONLY the mediamtx-webrtc proxy's `locations` line (or insert one),
# keeping every other byte. A block runs from a `[[proxies]]` header to the next
# one (or EOF); the target block is the one whose `name` is mediamtx-webrtc. Our
# template lists `name` before `locations`, so `target` is set before we reach it.
awk -v eid="$EDGE_ID" '
  function loc() { return "locations = [\"/" eid "\"]  # ceravis:edge-id" }
  /^[[:space:]]*\[\[proxies\]\]/ {
      if (target && !wrote) { print loc(); wrote=1 }   # block ended w/o locations
      target=0; print; next
  }
  /^[[:space:]]*name[[:space:]]*=/ {
      target = ($0 ~ /"mediamtx-webrtc"/) ? 1 : 0
      if (target) wrote=0
      print; next
  }
  (target && /^[[:space:]]*locations[[:space:]]*=/) { print loc(); wrote=1; next }
  { print }
  END { if (target && !wrote) print loc() }
' "$FRPC" > "$TMP"

# The target proxy must exist — refuse to touch a config with no mediamtx-webrtc
# block (wrong file / old template) rather than write a stray line.
grep -Eq '^[[:space:]]*name[[:space:]]*=[[:space:]]*"mediamtx-webrtc"' "$TMP" || {
    echo "no 'mediamtx-webrtc' proxy in $FRPC — not touching it" >&2; exit 4; }

# Idempotent: already correct -> no rewrite, no restart (keeps boot self-heal free).
if cmp -s "$TMP" "$FRPC"; then
    echo "frpc: mediamtx-webrtc already at /${EDGE_ID} — no change"
    exit 0
fi

# Never restart onto a broken config.
if command -v frpc >/dev/null 2>&1; then
    frpc verify -c "$TMP" >/dev/null 2>&1 || {
        echo "generated config failed 'frpc verify' — aborting" >&2; exit 5; }
fi

install -m 0644 "$TMP" "$FRPC"
systemctl restart frpc
echo "frpc: mediamtx-webrtc locations set to /${EDGE_ID}; restarted"
