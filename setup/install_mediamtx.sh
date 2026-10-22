#!/usr/bin/env bash
# Install MediaMTX — the media backbone CERAVIS supervises as a child process
# (one systemd service; no separate unit). Also generates a self-signed TLS cert
# for serving HLS/WebRTC over HTTPS in LAN-DIRECT mode — unique to this device
# (first boot makes a new one on every copied device).
#
# Fleet mode (DEVICE_STREAM_BASE set): TLS is terminated upstream at the cloud
# Caddy and the edge serves plaintext through the frp tunnel, so this cert is
# IGNORED (tls_enabled() short-circuits to plaintext) — harmless to leave.
#
# Offline: a release bundle may carry the binary as vendor/mediamtx_v<ver>_<arch>
# .tar.gz; it is used instead of downloading.
#
# Run once:  bash setup/install_mediamtx.sh
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MTX_VERSION="${MTX_VERSION:-1.9.3}"
MTX_BIN="/usr/local/bin/mediamtx"
CERT_DIR="$(data_dir)/certs"

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
    LOCAL="$CERAVIS_ROOT/vendor/mediamtx_v${MTX_VERSION}_${MTX_ARCH}.tar.gz"
    if [ -s "$LOCAL" ]; then
        cp "$LOCAL" "$TMP/mtx.tar.gz"
    else
        URL="https://github.com/bluenviron/mediamtx/releases/download/v${MTX_VERSION}/mediamtx_v${MTX_VERSION}_${MTX_ARCH}.tar.gz"
        curl -fL --retry 3 -o "$TMP/mtx.tar.gz" "$URL"
    fi
    tar -xzf "$TMP/mtx.tar.gz" -C "$TMP" mediamtx
    sudo install -m 0755 "$TMP/mediamtx" "$MTX_BIN"
    echo "Installed: $("$MTX_BIN" --version)"
fi

# ---- self-signed TLS cert (HLS/WebRTC over https on the LAN) --------
if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
    echo "TLS cert already present: $CERT_DIR"
else
    echo "Generating self-signed TLS cert…"
    sudo mkdir -p "$CERT_DIR"
    NAME="$(device_name 2>/dev/null || echo ceravis-edge)"
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
        -subj "/CN=$NAME" \
        -addext "subjectAltName=DNS:$NAME,DNS:$NAME.local,DNS:localhost,IP:127.0.0.1${IP:+,IP:$IP}" \
        >/dev/null 2>&1
    sudo chown -R "$CERAVIS_USER" "$CERT_DIR"
    sudo chmod 600 "$CERT_DIR/server.key"
    echo "Cert written to $CERT_DIR (CN=$NAME, valid 10y)."
fi

echo
echo "Done. MediaMTX is supervised by ceravis.service itself — just restart it:"
echo "  sudo systemctl restart ceravis"
echo "LAN-direct live links:  https://<jetson-ip>:8889/<camera-id>/whep  (WebRTC)"
echo "                        https://<jetson-ip>:8888/<camera-id>/index.m3u8  (HLS)"
echo "Fleet (remote) link:    https://<domain>/<edge_id>/<camera-id>/whep  (see cloud/)"
