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
# The same folder goes to a USB stick (--usb), straight to a device over the
# network (setup/send_to_device.sh), or — packed as ONE file with the keys
# encrypted (--package) — to someone far away. It never holds a secret: dist/ is inside
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
#   vendor/         the arm64 MediaMTX + frp binaries and virtualenv.pyz (kept
#                   in ~/.ceravis/vendor)
#   wheels/         every Python package the app needs, for the device (arm64,
#                   Python 3.10 — JetPack 6), resolved the way the device would
#                   resolve them, with requirements.lock (the exact versions);
#                   pycuda as source, built on the device. Kept in
#                   ~/.ceravis/wheels. With models-src/ and vendor/ this makes
#                   the install need NO internet (only the first sign-in, the
#                   cloud and remote access do)
#   debs/           the system packages the device's JetPack lacks, from the
#                   list the device itself printed into ~/.ceravis/debs.txt:
#                     sudo apt-get install --print-uris -qq --no-install-recommends \
#                         gstreamer1.0-plugins-bad gstreamer1.0-libav ffmpeg > debs.txt
#                   each file downloaded once into ~/.ceravis/debs and checked
#                   against the size and checksum in that list
#
# Options:
#   --models DIR     take the ONNX from DIR (default ~/.ceravis/models; any
#                    missing one is exported there first, in ~/.ceravis/export-venv)
#   --no-models      leave the models out (the device then exports them itself)
#   --no-vendor      leave the binaries out (the device downloads them)
#   --no-wheels      leave the Python packages out (the device downloads them)
#   --debs FILE      the device's package list (default ~/.ceravis/debs.txt, if there)
#   --no-debs        leave the system packages out (the device downloads them)
#   --usb DIR        also copy the folder, with the secrets and keys, to DIR/CERAVIS
#   --package        also pack it as ONE file to download — dist/CERAVIS-<version>.tar
#                    (+ .sha256): the folder, the keys ENCRYPTED with a passphrase
#                    made here (shown once, kept in ~/.ceravis/CERAVIS-<version>.passphrase,
#                    given by phone, never in the same message as the link),
#                    and the developers' SSH public keys
#   --relock         take the newest Python package versions requirements.txt
#                    allows (otherwise the ones already released stay — a new
#                    upload on PyPI never slips an untested version into a release)
#   --allow-dirty    build even with uncommitted changes (they are NOT included)
#
# Run:  bash setup/make_release.sh [--usb E:/]
# =============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PC="${CERAVIS_PC_DIR:-$HOME/.ceravis}"
MODELS="$PC/models"; WITH_MODELS=1; VENDOR=1; WHEELS=1; USB=""; DIRTY_OK=0; PACKAGE=0; RELOCK=0
DEBS_LIST="$PC/debs.txt"
while [ $# -gt 0 ]; do
    case "$1" in
        --models) MODELS="$2"; shift 2 ;;
        --no-models) WITH_MODELS=0; shift ;;
        --no-vendor) VENDOR=0; shift ;;
        --no-wheels) WHEELS=0; shift ;;
        --debs) DEBS_LIST="$2"; shift 2 ;;
        --no-debs) DEBS_LIST=""; shift ;;
        --usb) USB="$2"; shift 2 ;;
        --package) PACKAGE=1; shift ;;
        --allow-dirty) DIRTY_OK=1; shift ;;
        --relock) RELOCK=1; shift ;;
        -h|--help) sed -n '2,/^# =====/p' "${BASH_SOURCE[0]}" | sed '$d'; exit 0 ;;
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
rm -rf "$DIST/.stage" "$DIST/.pkg" "$OUT" "$DIST/usb" "$DIST"/ceravis-edge-*.tar.gz "$DIST"/ceravis-edge-*.sha256 \
       "$DIST"/CERAVIS-*.tar "$DIST"/CERAVIS-*.tar.sha256
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
             "frp_${FRP}_linux_arm64.tar.gz|https://github.com/fatedier/frp/releases/download/v${FRP}" \
             "virtualenv.pyz|https://bootstrap.pypa.io"; do
        file="${f%%|*}"
        [ -s "$PC/vendor/$file" ] || curl -fL --retry 3 -o "$PC/vendor/$file" "${f#*|}/$file"
        cp "$PC/vendor/$file" "$STAGE/vendor/"
    done
    echo "   vendor MediaMTX $MTX, frp $FRP, virtualenv"
fi

# The device's Python packages: resolved for the device itself (Linux arm64,
# Python 3.10, glibc 2.35 — JetPack 6 / Ubuntu 22.04) with uv, which applies the
# device's platform markers, then downloaded once into ~/.ceravis/wheels (a
# changed resolution starts the folder afresh). pycuda comes as source from
# PyPI — it is compiled on the device against its own CUDA.
if [ "$WHEELS" = 1 ]; then
    win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
    VENV="$PC/export-venv"
    [ -x "$VENV/bin/python" ] || [ -x "$VENV/Scripts/python.exe" ] \
        || "$PY" -m venv --system-site-packages "$VENV"
    VPY="$VENV/bin/python"; [ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"
    "$VPY" -c "import uv" 2>/dev/null || "$VPY" -m pip install -q uv
    CACHE="$PC/wheels/cp310-aarch64"
    mkdir -p "$PC/wheels"
    printf '%s\n' "-r $(win "$REPO/edge/requirements.txt")" "scipy>=1.8,<1.14" "pycuda" > "$PC/wheels/target.in"
    # The versions already released stay: the last lock seeds the resolution
    # (uv keeps every pin requirements.txt still allows), so a new upload on
    # PyPI never slips an untested version into a release. --relock: newest.
    rm -f "$PC/wheels/requirements.lock.new"
    [ "$RELOCK" = 1 ] || [ ! -s "$CACHE/requirements.lock" ] \
        || cp "$CACHE/requirements.lock" "$PC/wheels/requirements.lock.new"
    if "$VPY" -m uv pip compile "$(win "$PC/wheels/target.in")" -q --no-header --no-annotate \
            --python-platform aarch64-manylinux_2_35 --python-version 3.10 \
            -o "$(win "$PC/wheels/requirements.lock.new")"; then
        tr -d '\r' < "$PC/wheels/requirements.lock.new" > "$PC/wheels/requirements.lock.next"
        rm -f "$PC/wheels/requirements.lock.new"
        cmp -s "$PC/wheels/requirements.lock.next" "$CACHE/requirements.lock" || rm -rf "$CACHE"
        mkdir -p "$CACHE" && mv "$PC/wheels/requirements.lock.next" "$CACHE/requirements.lock"
    elif [ -s "$CACHE/requirements.lock" ]; then
        echo "   (could not resolve — no internet? — using the packages already in $CACHE)"
    else
        echo "cannot resolve the device's Python packages (internet needed once)" >&2; exit 1
    fi
    grep -v '^pycuda==' "$CACHE/requirements.lock" > "$PC/wheels/binary.txt"
    # Offline first: what the cache already holds is not asked of PyPI again (a
    # PyPI hiccup once failed a whole build); only what is missing is fetched.
    fetch() {
        "$VPY" -m pip download -q --no-deps -d "$CACHE" --no-index --find-links "$(win "$CACHE")" "$@" \
            2>/dev/null || "$VPY" -m pip download -q --no-deps -d "$CACHE" --retries 5 "$@"
    }
    fetch --only-binary=:all: \
        --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 \
        --platform manylinux_2_28_aarch64 --platform manylinux_2_35_aarch64 --platform linux_aarch64 \
        --python-version 3.10 --implementation cp --abi cp310 --abi abi3 --abi none \
        -r "$(win "$PC/wheels/binary.txt")"
    # pycuda's build needs setuptools + wheel (numpy is in the set already).
    fetch --only-binary=:all: --platform any --python-version 3.10 "setuptools>=64,<81" wheel
    PYCUDA="$(grep '^pycuda==' "$CACHE/requirements.lock" | cut -d= -f3)"
    if [ -n "$PYCUDA" ] && ! ls "$CACHE"/pycuda-"$PYCUDA".tar.gz >/dev/null 2>&1; then
        URL="$("$VPY" -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('https://pypi.org/pypi/pycuda/$PYCUDA/json')); print(next(u['url'] for u in d['urls'] if u['packagetype']=='sdist'))" | tr -d '\r')"
        curl -fsSL --retry 3 -o "$CACHE/$(basename "$URL")" "$URL"
    fi
    mkdir -p "$STAGE/wheels" && cp "$CACHE"/* "$STAGE/wheels/"
    echo "   wheels $(ls "$STAGE/wheels" | grep -c -v requirements.lock) Python packages for the device ($(du -sh "$STAGE/wheels" | cut -f1))"
fi

# The device's missing system packages: each line of its list is
#   'URL' FILE SIZE MD5Sum:<hex> | SHA256:<hex> | SHA512:<hex>
# (the checksums come from the device's own signed package index).
if [ -n "$DEBS_LIST" ] && [ -s "$DEBS_LIST" ]; then
    deb_ok() {      # <file> <size> <type:hex>
        local sum
        [ -s "$1" ] && [ "$(wc -c < "$1" | tr -d ' ')" = "$2" ] || return 1
        case "${3%%:*}" in
            MD5Sum) sum="$(md5sum "$1")" ;; SHA256) sum="$(sha256sum "$1")" ;;
            SHA512) sum="$(sha512sum "$1")" ;; *) return 1 ;;
        esac
        [ "${sum%% *}" = "${3#*:}" ]
    }
    mkdir -p "$PC/debs" "$STAGE/debs"
    n=0
    while read -r url file size sum; do
        url="${url//\'/}"
        [ -n "$file" ] || continue
        if ! deb_ok "$PC/debs/$file" "$size" "$sum"; then
            curl -fsSL --retry 3 -o "$PC/debs/$file" "$url"
            deb_ok "$PC/debs/$file" "$size" "$sum" \
                || { echo "package $file does not match the device's checksum — not used" >&2; exit 1; }
        fi
        cp "$PC/debs/$file" "$STAGE/debs/"
        n=$((n + 1))
    done < <(tr -d '\r' < "$DEBS_LIST")
    cp "$DEBS_LIST" "$STAGE/debs/debs.txt"
    echo "   debs   $n system package files for the device ($(du -sh "$STAGE/debs" | cut -f1))"
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

# ONE file for someone far away: the folder, the keys encrypted with a fresh
# passphrase (16 characters from 32 that cannot be misheard — 80 bits, then
# 200 000 rounds of PBKDF2), the developers' SSH public keys.
if [ "$PACKAGE" = 1 ]; then
    [ -s "$PC/ceravis-secrets.env" ] || { echo "no $PC/ceravis-secrets.env to pack" >&2; exit 1; }
    eval "$(grep '^CIPHER=' setup/usb/install.sh)"          # the folder installer's ONE setting
    ABC=ABCDEFGHJKLMNPQRSTUVWXYZ23456789
    PASS=""
    for b in $(od -An -tu1 -N16 /dev/urandom); do
        [ $(( ${#PASS} % 5 )) = 4 ] && PASS+="-"
        PASS+="${ABC:$((b % 32)):1}"
    done
    PKG="CERAVIS-$VERSION.tar"
    EXTRA=(CERAVIS/ceravis-secrets.env.enc)
    mkdir -p "$DIST/.pkg/CERAVIS"
    printf '%s\n' "$PASS" | openssl enc "${CIPHER[@]}" -salt -pass stdin \
        -in "$PC/ceravis-secrets.env" -out "$DIST/.pkg/CERAVIS/ceravis-secrets.env.enc"
    if [ -s "$PC/authorized_keys" ]; then
        cp "$PC/authorized_keys" "$DIST/.pkg/CERAVIS/" && EXTRA+=(CERAVIS/authorized_keys)
    fi
    tar -cf "$DIST/$PKG" -C "$DIST" CERAVIS -C "$DIST/.pkg" "${EXTRA[@]}"
    rm -rf "$DIST/.pkg"
    (cd "$DIST" && sha256sum "$PKG" > "$PKG.sha256")
    (umask 077 && printf '%s\n' "$PASS" > "$PC/CERAVIS-$VERSION.passphrase")
    echo "== ONE file to send: $DIST/$PKG  ($(du -h "$DIST/$PKG" | cut -f1))"
    echo "   sha256      $(cut -d' ' -f1 "$DIST/$PKG.sha256")"
    echo "   passphrase  $PASS   (also in $PC/CERAVIS-$VERSION.passphrase —"
    echo "               give it by phone, never with the link)"
fi
echo "Send it:  bash setup/send_to_device.sh ceravis@<device-ip>      (or --usb E:/, or --package)"
