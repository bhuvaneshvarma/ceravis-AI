#!/usr/bin/env bash
# =============================================================
# CERAVIS — install from this USB stick. The ONE command:
#
#     sudo bash /media/$USER/<stick name>/CERAVIS/install-from-usb.sh
#
# It checks the release file against its fingerprint (.sha256), unpacks it on
# the device and runs its installer, with ceravis-secrets.env from this stick
# when it is there. The stick is only read — take it out when it says done.
# =============================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" = 0 ] || exec sudo bash "$0" "$@"

BUNDLE="$(ls -1t "$HERE"/ceravis-edge-*.tar.gz 2>/dev/null | head -1)"
[ -n "$BUNDLE" ] || { echo "no ceravis-edge-*.tar.gz on this stick ($HERE)" >&2; exit 1; }
echo "== checking $(basename "$BUNDLE")"
(cd "$HERE" && sha256sum -c --quiet "$(basename "$BUNDLE").sha256") \
    || { echo "the release file is damaged or was changed — copy it to the stick again" >&2; exit 1; }

WORK=/var/tmp/ceravis-install
rm -rf "$WORK" && mkdir -p "$WORK"
echo "== unpacking"
tar -xzf "$BUNDLE" -C "$WORK"
TOP="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -1)"

ARGS=()
[ -f "$HERE/ceravis-secrets.env" ] && ARGS=(--secrets "$HERE/ceravis-secrets.env")
bash "$TOP/install.sh" ${ARGS[@]+"${ARGS[@]}"} "$@"
rm -rf "$WORK"
echo "== done — you can remove the USB stick"
