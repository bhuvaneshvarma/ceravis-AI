#!/usr/bin/env bash
# Apply this device's edge_id to EVERY per-edge key in frpc.toml and restart frpc:
#   mediamtx-webrtc  locations    = ["/<edge_id>"]                      (live WHEP)
#   ceravis-api      locations    = ["/<edge_id>/api|/ui"]              (uvicorn)
#   ceravis-ssh      customDomains = ["<edge_id>"]                      (fleet SSH)
# One edge_id, one command, every route — so no proxy can drift out of step.
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
#     404s every live link (the exact bug this guards against). The SSH proxy is
#     the mirror image: its CONNECT host is a HOSTNAME, never a path, so there it
#     writes the bare "<edge_id>" with NO slash. Same token, two shapes, both
#     machine-written so neither can be typed wrong.
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

# Rewrite the per-edge key of each proxy (or insert it), keeping every other byte:
#   mediamtx-webrtc -> locations     = ["/<edge_id>"]                       (WHEP)
#   ceravis-api     -> locations     = ["/<eid>/api", "/<eid>/ui"]
#   ceravis-ssh     -> customDomains = ["<edge_id>"]                  (tcpmux host)
# Only ceravis-ssh's customDomains is touched — the HTTP proxies' customDomains is
# the shared fleet DOMAIN and must never be rewritten. A block runs from a
# `[[proxies]]` header to the next (or EOF); our template lists `name` first, so
# the target flag is set before the line it governs. A commented-out block (every
# line behind a `#`) matches nothing here and is deliberately left alone: SSH stays
# opt-in per house, and this only maintains it once enabled.
awk -v eid="$EDGE_ID" '
  function mtx() { return "locations = [\"/" eid "\"]  # ceravis:edge-id" }
  function api() { return "locations = [\"/" eid "/api\", \"/" eid "/ui\"]  # ceravis:edge-id" }
  function ssh() { return "customDomains = [\"" eid "\"]  # ceravis:ssh-edge-id" }
  function flush_block() {
      if (wrote) return
      if (tmtx) print mtx(); else if (tapi) print api(); else if (tssh) print ssh()
  }
  /^[[:space:]]*\[\[proxies\]\]/ { flush_block(); tmtx=0; tapi=0; tssh=0; print; next }
  /^[[:space:]]*name[[:space:]]*=/ {
      tmtx = ($0 ~ /"mediamtx-webrtc"/)
      tapi = ($0 ~ /"ceravis-api"/)
      tssh = ($0 ~ /"ceravis-ssh"/)
      if (tmtx || tapi || tssh) wrote=0
      print; next
  }
  ((tmtx || tapi) && /^[[:space:]]*locations[[:space:]]*=/) {
      print (tmtx ? mtx() : api()); wrote=1; next
  }
  (tssh && /^[[:space:]]*customDomains[[:space:]]*=/) { print ssh(); wrote=1; next }
  { print }
  END { flush_block() }
' "$FRPC" > "$TMP"

# The live-link proxy must exist — refuse to touch a config with no
# mediamtx-webrtc block (wrong file / old template) rather than write stray lines.
grep -Eq '^[[:space:]]*name[[:space:]]*=[[:space:]]*"mediamtx-webrtc"' "$TMP" || {
    echo "no 'mediamtx-webrtc' proxy in $FRPC — not touching it" >&2; exit 4; }

# Idempotent: already correct -> no rewrite, no restart (keeps boot self-heal free).
if cmp -s "$TMP" "$FRPC"; then
    echo "frpc: every proxy already keyed to ${EDGE_ID} — no change"
    exit 0
fi

# Never restart onto a broken config.
if command -v frpc >/dev/null 2>&1; then
    frpc verify -c "$TMP" >/dev/null 2>&1 || {
        echo "generated config failed 'frpc verify' — aborting" >&2; exit 5; }
fi

install -m 0644 "$TMP" "$FRPC"
systemctl restart frpc
echo "frpc: proxies keyed to ${EDGE_ID} (live /${EDGE_ID}, api /${EDGE_ID}/api,"
echo "      ssh CONNECT host ${EDGE_ID} if enabled); restarted"
