#!/usr/bin/env bash
# Install the frp CLIENT on the Jetson edge: the binary, its config, its unit,
# and the privileged helper that keys the tunnel to the device's edge_id at
# account verification. Never stops an install: with no server details yet the
# tunnel is installed but stays off (frpc.service starts only on a real config).
#
# Where the config comes from, first match wins:
#   1. cloud/frpc.toml next to this script (a hand-made one — development)
#   2. FRP_SERVER_ADDR + FRP_TOKEN in the environment (install.sh passes them
#      from the secrets file) — built from cloud/frpc.toml.example
#   3. an existing /etc/frp/frpc.toml — kept as it is
#   4. otherwise the template with its placeholders — the tunnel stays off
# Every proxy is then prefixed with this device's own name (frpc `user`), so no
# two homes ever share a proxy name on the server.
#
# Run:  bash cloud/install_frpc.sh
set -euo pipefail

FRP_VERSION="${FRP_VERSION:-0.61.1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup/lib.sh
. "$HERE/../setup/lib.sh"
CONF=/etc/frp/frpc.toml

case "$(uname -m)" in
    aarch64) FA=arm64 ;;      # Jetson
    x86_64)  FA=amd64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
esac

# ---- binary (offline from the bundle's vendor/ when present) -------------
if command -v frpc >/dev/null 2>&1 && frpc --version 2>/dev/null | grep -qx "$FRP_VERSION"; then
    echo "frpc v$FRP_VERSION already installed."
else
    echo "Installing frpc v$FRP_VERSION ($FA)…"
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    LOCAL="$CERAVIS_ROOT/vendor/frp_${FRP_VERSION}_linux_${FA}.tar.gz"
    if [ -s "$LOCAL" ]; then
        cp "$LOCAL" "$TMP/frp.tgz"
    else
        curl -fL --retry 3 -o "$TMP/frp.tgz" \
            "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${FA}.tar.gz"
    fi
    tar -xzf "$TMP/frp.tgz" -C "$TMP"
    sudo install -m0755 "$TMP/frp_${FRP_VERSION}_linux_${FA}/frpc" /usr/local/bin/frpc
fi

# ---- config -----------------------------------------------------------
sudo mkdir -p /etc/frp
if [ -f "$HERE/frpc.toml" ]; then
    sudo cp "$HERE/frpc.toml" "$CONF"
    echo "config: cloud/frpc.toml"
elif [ -n "${FRP_SERVER_ADDR:-}" ] && [ -n "${FRP_TOKEN:-}" ]; then
    case "$FRP_SERVER_ADDR$FRP_TOKEN" in
        *[!A-Za-z0-9._:~+/=-]*) echo "refusing: unexpected characters in FRP_SERVER_ADDR/FRP_TOKEN" >&2; exit 2 ;;
    esac
    sed -e "s|\"EC2_PUBLIC_IP_OR_DOMAIN\"|\"$FRP_SERVER_ADDR\"|" \
        -e "s|\"CHANGE_ME_TO_A_LONG_RANDOM_SECRET\"|\"$FRP_TOKEN\"|" \
        "$HERE/frpc.toml.example" | sudo tee "$CONF" >/dev/null
    echo "config: built from the template for $FRP_SERVER_ADDR"
elif [ -f "$CONF" ]; then
    echo "config: keeping $CONF"
else
    sudo cp "$HERE/frpc.toml.example" "$CONF"
    echo "config: template only — the tunnel stays OFF until a server and token are set"
    echo "        (re-run with FRP_SERVER_ADDR=<addr> FRP_TOKEN=<token>, or edit $CONF)"
fi
# It holds the tunnel token: root writes it, the service's group may only read
# it (integration/edge_provision checks the routes), nobody else.
sudo chown "root:$(id -gn "$CERAVIS_USER")" "$CONF"
sudo chmod 0640 "$CONF"
NAME="$(device_name)"
frpc_set_user "$CONF" "$NAME"
echo "proxies prefixed with this device's name: $NAME"

install_unit "$HERE/systemd/frpc.service"
sudo systemctl daemon-reload
sudo systemctl enable frpc >/dev/null
# Starts only when the config is real (ExecCondition in the unit): a template, or
# routes still waiting for an account, leave it off without an error.
sudo systemctl restart frpc || true

# Privileged helper the edge app uses to apply the edge_id to the tunnel at
# account verification (integration/edge_provision). Root-owned, plus a NOPASSWD
# sudoers rule for EXACTLY this one command — so the unprivileged app can update
# frpc's locations + restart it, and nothing else.
sudo install -m0755 "$HERE/apply_edge_id.sh" /usr/local/bin/ceravis-apply-edge-id
install_sudoers ceravis-frpc "$CERAVIS_USER ALL=(root) NOPASSWD: /usr/local/bin/ceravis-apply-edge-id" \
    || echo "WARNING: the tunnel will not follow the account's edge_id automatically"

echo
echo "frpc: $(systemctl is-active frpc 2>/dev/null || true)  (inactive = waiting for a server or an account)"
echo "Check:  sudo systemctl status frpc   |   journalctl -u frpc -f"
echo "edge_id is applied AUTOMATICALLY at account verification: the app runs"
echo "ceravis-apply-edge-id, which keys every proxy to it and starts the tunnel."
echo "Helper runs as: $CERAVIS_USER"
