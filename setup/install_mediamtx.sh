#!/usr/bin/env bash
# Install MediaMTX — the media backbone CERAVIS supervises as a child process
# (one systemd service; no separate unit). Also generates a self-signed TLS cert
# for serving HLS/WebRTC over HTTPS in LAN-DIRECT mode.
#
# Fleet mode (DEVICE_STREAM_BASE set): TLS is terminated upstream at the cloud
# Caddy and the edge serves plaintext through the frp tunnel, so this cert is
# IGNORED (tls_enabled() short-circuits to plaintext) — harmless to leave.
#
# Run once:  bash setup/install_mediamtx.sh
set -euo pipefail

MTX_VERSION="${MTX_VERSION:-1.9.3}"
MTX_BIN="/usr/local/bin/mediamtx"

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SETUP_DIR")"
CERT_DIR="$REPO_DIR/edge/data/certs"

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64) MTX_ARCH="linux_arm64v8" ;;
    x86_64)  MTX_ARCH="linux_amd64"   ;;
    *) echo "unsupported arch: $ARCH"; exit 1 ;;
esac

# ---- binary ---------------------------------------------------------
if [ -x "$MTX_BIN" ] && "$MTX_BIN" --version 2>/dev/null | grep -q "$MTX_VERSION"; then
    echo "MediaMTX v$MTX_VERSION already installed."
else
    echo "Installing MediaMTX v$MTX_VERSION ($MTX_ARCH)…"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    URL="https://github.com/bluenviron/mediamtx/releases/download/v${MTX_VERSION}/mediamtx_v${MTX_VERSION}_${MTX_ARCH}.tar.gz"
    curl -fL --retry 3 -o "$TMP/mtx.tar.gz" "$URL"
    tar -xzf "$TMP/mtx.tar.gz" -C "$TMP" mediamtx
    sudo install -m 0755 "$TMP/mediamtx" "$MTX_BIN"
    echo "Installed: $("$MTX_BIN" --version)"
fi

# ---- self-signed TLS cert (HLS/WebRTC over https on the LAN) --------
if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
    echo "TLS cert already present: $CERT_DIR"
else
    echo "Generating self-signed TLS cert…"
    mkdir -p "$CERT_DIR"
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
        -subj "/CN=ceravis-edge" \
        -addext "subjectAltName=DNS:ceravis-edge,DNS:localhost,IP:127.0.0.1${IP:+,IP:$IP}" \
        >/dev/null 2>&1
    chmod 600 "$CERT_DIR/server.key"
    echo "Cert written to $CERT_DIR (CN=ceravis-edge, valid 10y)."
fi

echo
echo "Done. MediaMTX is supervised by ceravis.service itself — just restart it:"
echo "  sudo systemctl restart ceravis"
echo "LAN-direct live links:  https://<jetson-ip>:8889/<camera-id>/whep  (WebRTC)"
echo "                        https://<jetson-ip>:8888/<camera-id>/index.m3u8  (HLS)"
echo "Fleet (remote) link:    https://<domain>/<edge_id>/<camera-id>/whep  (see cloud/)"
