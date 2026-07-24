#!/usr/bin/env bash
# Install the frp CLIENT on the Jetson edge. Run from the cloud/ folder:
#   cd ~/ceravis/cloud && cp frpc.toml.example frpc.toml && nano frpc.toml
#   bash install_frpc.sh
# Needs frpc.toml (copied from frpc.toml.example, with serverAddr + auth.token
# filled in) next to this script.
set -euo pipefail

FRP_VERSION="${FRP_VERSION:-0.61.1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$HERE/frpc.toml" ]; then
    echo "ERROR: $HERE/frpc.toml missing."
    echo "  cp frpc.toml.example frpc.toml   then edit serverAddr + auth.token"
    exit 1
fi

case "$(uname -m)" in
    aarch64) FA=arm64 ;;      # Jetson
    x86_64)  FA=amd64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
esac

echo "Installing frpc v$FRP_VERSION ($FA)…"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fL --retry 3 -o "$TMP/frp.tgz" \
    "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${FA}.tar.gz"
tar -xzf "$TMP/frp.tgz" -C "$TMP"
sudo install -m0755 "$TMP/frp_${FRP_VERSION}_linux_${FA}/frpc" /usr/local/bin/frpc

sudo mkdir -p /etc/frp
sudo cp "$HERE/frpc.toml" /etc/frp/frpc.toml
sudo cp "$HERE/systemd/frpc.service" /etc/systemd/system/frpc.service
sudo systemctl daemon-reload
sudo systemctl enable --now frpc

# Privileged helper the edge app uses to apply the edge_id to the tunnel at
# account verification (integration/edge_provision). Root-owned, plus a NOPASSWD
# sudoers rule for EXACTLY this one command — so the unprivileged app can update
# frpc's locations + restart it, and nothing else.
sudo install -m0755 "$HERE/apply_edge_id.sh" /usr/local/bin/ceravis-apply-edge-id
SVC_USER="$(grep -oP '^User=\K\S+' /etc/systemd/system/ceravis.service 2>/dev/null || true)"
SVC_USER="${SVC_USER:-${SUDO_USER:-$USER}}"
printf '%s ALL=(root) NOPASSWD: /usr/local/bin/ceravis-apply-edge-id\n' "$SVC_USER" \
    | sudo tee /etc/sudoers.d/ceravis-frpc >/dev/null
sudo chmod 0440 /etc/sudoers.d/ceravis-frpc
sudo visudo -cf /etc/sudoers.d/ceravis-frpc >/dev/null \
    || { echo "WARNING: sudoers rule invalid — removing it"; sudo rm -f /etc/sudoers.d/ceravis-frpc; }

echo
echo "frpc running. Check:  sudo systemctl status frpc   |   journalctl -u frpc -f"
echo "Healthy log shows 'start proxy success' for mediamtx-webrtc + ceravis-api."
echo
echo "edge_id is applied AUTOMATICALLY now: when you verify the account in the"
echo "setup wizard, the app writes EDGE_ID to jetson.env and runs"
echo "ceravis-apply-edge-id (installed above) to set the tunnel's locations and"
echo "restart frpc — no manual edit. Helper runs as: $SVC_USER"
