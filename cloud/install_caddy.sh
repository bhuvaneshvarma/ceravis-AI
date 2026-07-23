#!/usr/bin/env bash
# Install Caddy (the fleet TLS front) on the AWS EC2 box. Run from the cloud/
# folder:
#   cd ~/ceravis-cloud && cp Caddyfile.example Caddyfile && nano Caddyfile
#   bash install_caddy.sh
# Needs Caddyfile (copied from Caddyfile.example, domain edited) next to this
# script, and frps already running (install_frps.sh). Caddy reverse-proxies to
# the frps HTTP vhost on 127.0.0.1:7080.
set -euo pipefail

CADDY_VERSION="${CADDY_VERSION:-2.8.4}"     # bump if this tag 404s
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$HERE/Caddyfile" ]; then
    echo "ERROR: $HERE/Caddyfile missing."
    echo "  cp Caddyfile.example Caddyfile   then edit the domain"
    exit 1
fi

case "$(uname -m)" in
    x86_64)  CA=amd64 ;;
    aarch64) CA=arm64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
esac

echo "Installing Caddy v$CADDY_VERSION ($CA)…"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fL --retry 3 -o "$TMP/caddy.tgz" \
    "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${CA}.tar.gz"
tar -xzf "$TMP/caddy.tgz" -C "$TMP" caddy
sudo install -m0755 "$TMP/caddy" /usr/local/bin/caddy

# Dedicated system user + config/state dirs (matches Caddy's packaged layout;
# certs land under /var/lib/caddy). Ignore "already exists".
sudo useradd --system --home-dir /var/lib/caddy --create-home caddy 2>/dev/null || true
sudo mkdir -p /etc/caddy /var/lib/caddy
sudo cp "$HERE/Caddyfile" /etc/caddy/Caddyfile
sudo chown -R caddy:caddy /var/lib/caddy

sudo cp "$HERE/systemd/caddy.service" /etc/systemd/system/caddy.service
sudo systemctl daemon-reload
sudo systemctl enable --now caddy

echo
echo "Caddy running. Check:  sudo systemctl status caddy   |   journalctl -u caddy -f"
echo "The first HTTPS request triggers the Let's Encrypt cert — needs the A"
echo "record pointed at this box and TCP 80/443 open."
echo "EC2 security-group inbound rules for the fleet:"
echo "  TCP 80, 443  (Caddy — ACME challenge + HTTPS entry)"
echo "  TCP 7000     (edges connect to frps)"
echo "  Keep 7080 CLOSED (frps vhost is loopback-only, behind Caddy)."
