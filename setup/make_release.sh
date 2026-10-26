#!/usr/bin/env bash
# =============================================================
# Build a CERAVIS release on the PC (Git Bash) or any Linux box — ONE folder:
#
#   dist/CERAVIS/
#     install.sh                          the one command on the device
#     ceravis-edge-<version>.tar.gz       the release
#     ceravis-edge-<version>.tar.gz.sha256  its fingerprint
#     README.txt                          what to do with it
#
# The same folder goes to a USB stick (--usb) or straight to a device over the
# network (setup/send_to_device.sh). It never holds a secret: dist/ is inside
# the synced project folder. The secrets and the developers' SSH keys live on
# this PC only, in ~/.ceravis/, and are added at delivery:
#   ~/.ceravis/ceravis-secrets.env   the keys (filled in once; template written
#                                    there the first time)
#   ~/.ceravis/authorized_keys       developers' SSH public keys
#
# Inside the release: exactly the committed code of HEAD (git archive — no
# history, no local edits, and none of the files .gitattributes marks
# export-ignore: tests, dev tools, cloud-server files, test pages), plus
#   manifest.json   version, commit, branch, build time, SHA-256 of every file
#   install.sh      the release's own installer:  sudo bash install.sh
#   models-src/     every ONNX model (setup/export_models.py's list) — made on
#                   this PC once and kept in ~/.ceravis/models, so the device
#                   only builds its TensorRT engines: no internet, no torch
#   vendor/         the arm64 MediaMTX + frp binaries (kept in ~/.ceravis/vendor)
#
# Options:
#   --models DIR     take the ONNX from DIR (default ~/.ceravis/models; any
#                    missing one is exported there first, in ~/.ceravis/export-venv)
#   --no-models      leave the models out (the device then exports them itself)
#   --no-vendor      leave the binaries out (the device downloads them)
#   --usb DIR        also copy the folder, with the secrets and keys, to DIR/CERAVIS
#   --allow-dirty    build even with uncommitted changes (they are NOT included)
#
# Run:  bash setup/make_release.sh [--usb E:/]
# =============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PC="${CERAVIS_PC_DIR:-$HOME/.ceravis}"
MODELS="$PC/models"; WITH_MODELS=1; VENDOR=1; USB=""; DIRTY_OK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --models) MODELS="$2"; shift 2 ;;
        --no-models) WITH_MODELS=0; shift ;;
        --no-vendor) VENDOR=0; shift ;;
        --usb) USB="$2"; shift 2 ;;
        --allow-dirty) DIRTY_OK=1; shift ;;
        -h|--help) sed -n '2,39p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

cd "$REPO"
# shellcheck source=setup/lib.sh
CERAVIS_LAYOUT_FILE=/nonexistent . "$REPO/setup/lib.sh"      # host_python only
PY="$(host_python)"
VERSION="$(tr -d '[:space:]' < edge/VERSION)"
COMMIT="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    [ "$DIRTY_OK" = 1 ] || { echo "uncommitted changes — commit them, or --allow-dirty (they are left out)" >&2; exit 1; }
    echo "note: uncommitted changes are NOT in the bundle"
fi

NAME="ceravis-edge-$VERSION"
DIST="${CERAVIS_DIST:-$REPO/dist}"
OUT="$DIST/CERAVIS"
STAGE="$DIST/.stage/$NAME"
# Earlier outputs go: the one folder below is the release.
rm -rf "$DIST/.stage" "$OUT" "$DIST/usb" "$DIST"/ceravis-edge-*.tar.gz "$DIST"/ceravis-edge-*.sha256
mkdir -p "$STAGE" "$OUT"
echo "== $NAME  ($BRANCH @ $COMMIT)"

git archive --worktree-attributes --format=tar HEAD | tar -x -C "$STAGE"
cat > "$STAGE/install.sh" <<'EOF'
#!/usr/bin/env bash
# CERAVIS — install this release:  sudo bash install.sh   (see setup/install.sh)
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/setup/install.sh" "$@"
EOF

if [ "$WITH_MODELS" = 1 ]; then
    # (tr: Python on Windows ends its lines with CRLF)
    list() { MODELS_DIR="$MODELS" CERAVIS_LAYOUT_FILE=/nonexistent "$PY" setup/export_models.py --list | tr -d '\r'; }
    if list | grep -q '^missing '; then
        echo "== exporting the missing ONNX models into $MODELS (once; kept for the next release)"
        mkdir -p "$MODELS"
        MODELS_DIR="$MODELS" CERAVIS_MODELS_DIR="$MODELS" CERAVIS_LAYOUT_FILE=/nonexistent \
            VENV="$PC/export-venv" bash setup/export_engines.sh --onnx-only
    fi
    list | grep '^missing ' && { echo "ONNX models still missing — see above" >&2; exit 1; }
    while IFS= read -r rel; do
        mkdir -p "$STAGE/models-src/$(dirname "$rel")"
        cp "$MODELS/$rel" "$STAGE/models-src/$rel"
        echo "   model  $rel"
    done < <(list)
fi

if [ "$VENDOR" = 1 ]; then
    MTX="$(grep -oE 'MTX_VERSION:-[0-9.]+' setup/install_mediamtx.sh | cut -d- -f2)"
    FRP="$(grep -oE 'FRP_VERSION:-[0-9.]+' cloud/install_frpc.sh | cut -d- -f2)"
    mkdir -p "$PC/vendor" "$STAGE/vendor"
    for f in "mediamtx_v${MTX}_linux_arm64v8.tar.gz|https://github.com/bluenviron/mediamtx/releases/download/v${MTX}" \
             "frp_${FRP}_linux_arm64.tar.gz|https://github.com/fatedier/frp/releases/download/v${FRP}"; do
        file="${f%%|*}"
        [ -s "$PC/vendor/$file" ] || curl -fL --retry 3 -o "$PC/vendor/$file" "${f#*|}/$file"
        cp "$PC/vendor/$file" "$STAGE/vendor/"
    done
    echo "   vendor MediaMTX $MTX, frp $FRP"
fi

# manifest.json: what this is, and the fingerprint of every file in it.
(
    cd "$STAGE"
    printf '{\n  "name": "%s",\n  "version": "%s",\n  "commit": "%s",\n  "branch": "%s",\n  "built": "%s",\n  "files": {\n' \
        "$NAME" "$VERSION" "$COMMIT" "$BRANCH" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    find . -type f ! -name manifest.json | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do
        printf '    "%s": "%s",\n' "$f" "$(sha256sum "$f" | cut -d' ' -f1)"
    done | sed '$ s/,$//'
    printf '  }\n}\n'
) > "$STAGE/manifest.json.tmp" && mv "$STAGE/manifest.json.tmp" "$STAGE/manifest.json"

# Model weights barely shrink at any level: the fastest gzip is as small and
# minutes quicker.
echo "== compressing"
tar -C "$DIST/.stage" -cf - "$NAME" | gzip -1 > "$OUT/$NAME.tar.gz"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
rm -rf "$DIST/.stage"
cp setup/usb/install.sh setup/usb/README.txt "$OUT/"
echo "== $OUT  ($(du -sh "$OUT" | cut -f1))"
echo "   sha256 $(cut -d' ' -f1 "$OUT/$NAME.tar.gz.sha256")"

# The PC's own secrets file: a template the first time, to fill in once.
if [ ! -e "$PC/ceravis-secrets.env" ]; then
    mkdir -p "$PC" && cp setup/usb/ceravis-secrets.env.example "$PC/ceravis-secrets.env"
    echo "   NEW: $PC/ceravis-secrets.env — fill it in once (it stays on this PC)"
fi

if [ -n "$USB" ]; then
    rm -rf "$USB/CERAVIS" && cp -r "$OUT" "$USB/CERAVIS"
    for f in ceravis-secrets.env authorized_keys; do
        [ -s "$PC/$f" ] && cp "$PC/$f" "$USB/CERAVIS/$f"
    done
    echo "== USB installer written to $USB/CERAVIS (with the secrets and SSH keys) — keep the stick safe"
fi
echo "Send it:  bash setup/send_to_device.sh ceravis@<device-ip>      (or --usb E:/ next time)"
