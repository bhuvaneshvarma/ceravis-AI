#!/usr/bin/env bash
# =============================================================
# Build a CERAVIS release bundle — on the PC (Git Bash) or any Linux box.
#
#   dist/ceravis-edge-<version>.tar.gz        the release (one file)
#   dist/ceravis-edge-<version>.tar.gz.sha256 its fingerprint
#
# Inside: exactly the committed code of HEAD (git archive — no history, no
# local edits, no secrets, and none of the files .gitattributes marks
# export-ignore: tests, dev tools, cloud-server files, test pages), plus
#   manifest.json   version, commit, branch, build time, SHA-256 of every file
#   install.sh      the one command:  sudo bash install.sh
# and optionally
#   models-src/     ready ONNX models (--onnx-from) — the device then only builds
#                   its TensorRT engines and needs no torch export at all
#   vendor/         MediaMTX + frp binaries (--vendor) for an offline install
#
# Options:
#   --onnx-from DIR   copy *.onnx from a models folder, e.g. a device's
#                     /opt/ceravis/current/models or a clone's edge/models
#   --vendor          download the arm64 MediaMTX + frp archives into vendor/
#   --usb DIR         also write the USB installer folder DIR/CERAVIS/
#   --allow-dirty     build even with uncommitted changes (they are NOT included)
#
# Run:  bash setup/make_release.sh [--onnx-from DIR] [--vendor] [--usb E:/]
# =============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ONNX_FROM=""; VENDOR=0; USB=""; DIRTY_OK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --onnx-from) ONNX_FROM="$2"; shift 2 ;;
        --vendor) VENDOR=1; shift ;;
        --usb) USB="$2"; shift 2 ;;
        --allow-dirty) DIRTY_OK=1; shift ;;
        -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

cd "$REPO"
VERSION="$(tr -d '[:space:]' < edge/VERSION)"
COMMIT="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    [ "$DIRTY_OK" = 1 ] || { echo "uncommitted changes — commit them, or --allow-dirty (they are left out)" >&2; exit 1; }
    echo "note: uncommitted changes are NOT in the bundle"
fi

NAME="ceravis-edge-$VERSION"
DIST="$REPO/dist"
STAGE="$DIST/$NAME"
rm -rf "$STAGE" && mkdir -p "$STAGE"
echo "== $NAME  ($BRANCH @ $COMMIT)"

git archive --worktree-attributes --format=tar HEAD | tar -x -C "$STAGE"
cat > "$STAGE/install.sh" <<'EOF'
#!/usr/bin/env bash
# CERAVIS — install this release:  sudo bash install.sh   (see setup/install.sh)
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/setup/install.sh" "$@"
EOF

if [ -n "$ONNX_FROM" ]; then
    [ -d "$ONNX_FROM" ] || { echo "no such folder: $ONNX_FROM" >&2; exit 1; }
    for rel in detection/yolo26m.onnx pose/yolo26m-pose.onnx reid/reid.onnx \
               face/face_detection_yunet_2022mar.onnx face/auraface_glintr100.onnx; do
        if [ -s "$ONNX_FROM/$rel" ]; then
            mkdir -p "$STAGE/models-src/$(dirname "$rel")"
            cp "$ONNX_FROM/$rel" "$STAGE/models-src/$rel"
            echo "   model  $rel"
        else
            echo "   (no $rel in $ONNX_FROM — the device will export/fetch it)"
        fi
    done
fi

if [ "$VENDOR" = 1 ]; then
    MTX="$(grep -oE 'MTX_VERSION:-[0-9.]+' setup/install_mediamtx.sh | cut -d- -f2)"
    FRP="$(grep -oE 'FRP_VERSION:-[0-9.]+' cloud/install_frpc.sh | cut -d- -f2)"
    mkdir -p "$STAGE/vendor"
    curl -fL --retry 3 -o "$STAGE/vendor/mediamtx_v${MTX}_linux_arm64v8.tar.gz" \
        "https://github.com/bluenviron/mediamtx/releases/download/v${MTX}/mediamtx_v${MTX}_linux_arm64v8.tar.gz"
    curl -fL --retry 3 -o "$STAGE/vendor/frp_${FRP}_linux_arm64.tar.gz" \
        "https://github.com/fatedier/frp/releases/download/v${FRP}/frp_${FRP}_linux_arm64.tar.gz"
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

tar -czf "$DIST/$NAME.tar.gz" -C "$DIST" "$NAME"
(cd "$DIST" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
rm -rf "$STAGE"
echo "== $DIST/$NAME.tar.gz  ($(du -h "$DIST/$NAME.tar.gz" | cut -f1))"
echo "   sha256 $(cut -d' ' -f1 "$DIST/$NAME.tar.gz.sha256")"

if [ -n "$USB" ]; then
    OUT="$USB/CERAVIS"
    mkdir -p "$OUT"
    cp "$DIST/$NAME.tar.gz" "$DIST/$NAME.tar.gz.sha256" "$OUT/"
    cp setup/usb/install-from-usb.sh setup/usb/README.txt "$OUT/"
    [ -f "$OUT/ceravis-secrets.env" ] || cp setup/usb/ceravis-secrets.env.example "$OUT/"
    echo "== USB installer written to $OUT"
    echo "   Add the secrets: rename ceravis-secrets.env.example -> ceravis-secrets.env and fill it in."
fi
