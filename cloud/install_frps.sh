#!/usr/bin/env bash
# Install the frp SERVER on the AWS EC2 box. Run from the cloud/ folder:
#   cd ~/ceravis-cloud && bash install_frps.sh
# Assumes frps.toml (with your edited auth.token) sits next to this script.
set -euo pipefail

FRP_VERSION="${FRP_VERSION:-0.61.1}"     # bump if this tag 404s
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -m)" in
    x86_64)  FA=amd64 ;;
    aarch64) FA=arm64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
esac

echo "Installing frps v$FRP_VERSION ($FA)…"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fL --retry 3 -o "$TMP/frp.tgz" \
    "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${FA}.tar.gz"
tar -xzf "$TMP/frp.tgz" -C "$TMP"
sudo install -m0755 "$TMP/frp_${FRP_VERSION}_linux_${FA}/frps" /usr/local/bin/frps

sudo mkdir -p /etc/frp
sudo cp "$HERE/frps.toml" /etc/frp/frps.toml
sudo cp "$HERE/systemd/frps.service" /etc/systemd/system/frps.service
sudo systemctl daemon-reload
sudo systemctl enable --now frps

echo
echo "frps running. Check:  sudo systemctl status frps   |   journalctl -u frps -f"
echo "Open these inbound ports in the EC2 security group:"
echo "  TCP 7000     (edges connect to frps)"
echo "  TCP 80, 443  (Caddy fleet TLS — run install_caddy.sh next)"
echo "  TCP 22       (SSH, your IP only)"
echo "  Keep 7080 CLOSED (frps HTTP vhost; Caddy reaches it over loopback)."
echo
echo "Next: cp Caddyfile.example Caddyfile, edit the domain, then bash install_caddy.sh"
