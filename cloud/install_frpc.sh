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

echo
echo "frpc running. Check:  sudo systemctl status frpc   |   journalctl -u frpc -f"
echo "A healthy log shows: 'start proxy success' for mediamtx-webrtc."
echo
echo "Next: set these in edge/infra/env/jetson.env and restart ceravis —"
echo "  DEVICE_STREAM_BASE=https://<EC2_PUBLIC_IP>"
echo "  MEDIAMTX_STUN_SERVER=stun:stun.l.google.com:19302"
echo "then re-sync cameras so the server stores the new global links."
