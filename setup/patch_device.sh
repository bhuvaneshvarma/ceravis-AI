#!/usr/bin/env bash
# =============================================================
# CERAVIS — patch a few files of an INSTALLED release (production layout,
# /opt/ceravis/current) to the versions of a newer commit, over SSH, without a
# new release. For a small fix that cannot wait for the next one.
#
#   curl -fsSL https://raw.githubusercontent.com/bhuvaneshvarma/ceravis-AI/<TO>/setup/patch_device.sh \
#     | bash -s -- <FROM> <TO> <file> [<file> …]
#
#   FROM  the commit the device runs now (the release it was installed from)
#   TO    the commit with the fix (on the production branch)
#   file  paths in the repo, e.g. edge/api/recipient_routes.py
#
# Safe by construction:
#   * every file is fetched at BOTH commits first; each file on the device
#     must equal the FROM version (or already the TO version) — anything else
#     stops it before a single file is changed
#   * the old files are kept in /var/backups/ceravis-patch-<time>/ (with
#     ROLLBACK.sh); owner, group and mode of each file are kept
#   * the service is restarted and must answer /health again
#
# Undo:  sudo bash /var/backups/ceravis-patch-<time>/ROLLBACK.sh
#
# The commits are pinned hashes, so what is fetched can never change under
# you. A file the patch would ADD (not in the release) is refused: that is a
# new release (setup/update.sh), not a patch.
# =============================================================
set -euo pipefail

REPO_RAW="${CERAVIS_RAW:-https://raw.githubusercontent.com/bhuvaneshvarma/ceravis-AI}"
APP="${CERAVIS_APP:-/opt/ceravis/current}"
BACKUPS="${CERAVIS_BACKUPS:-/var/backups}"
SERVICE="${CERAVIS_SERVICE:-ceravis}"
HEALTH="${CERAVIS_HEALTH:-http://127.0.0.1:8000/health}"
HEALTH_SECS="${CERAVIS_HEALTH_SECS:-120}"

say() { echo "== $*"; }
# Same content, line endings aside: a release built on Windows may carry a
# text file with other line endings than the repository has (setup.html came
# out of the 1.3.0 build with \r\r\n), and that is still the same version.
same() { cmp -s <(tr -d '\r' < "$1") <(sudo cat "$2" | tr -d '\r'); }
die() { echo "STOP: $*" >&2; exit 1; }

[ $# -ge 3 ] || die "usage: patch_device.sh <FROM> <TO> <file> [<file> …]"
FROM="$1"; TO="$2"; shift 2
for ref in "$FROM" "$TO"; do
    [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]] || die "'$ref' is not a commit hash (a branch name could move)"
done
for f in "$@"; do
    case "$f" in /*|*..*|"") die "'$f' is not a path inside the repo" ;; esac
done
[ -d "$APP" ] || die "$APP not found — this is not a production install"

sudo true                                   # ask the password once, up front
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
key() { echo "${1//\//__}"; }

say "fetching ${#} file(s) at $FROM and $TO"
TODO=()
for f in "$@"; do
    sudo test -f "$APP/$f" || die "$APP/$f does not exist — a new file needs a release, not a patch"
done
for f in "$@"; do
    k="$(key "$f")"
    curl -fsSL --retry 3 -o "$WORK/$k.from" "$REPO_RAW/$FROM/$f" \
        || die "could not fetch $f at $FROM"
    curl -fsSL --retry 3 -o "$WORK/$k.to" "$REPO_RAW/$TO/$f" \
        || die "could not fetch $f at $TO"
    if same "$WORK/$k.to" "$APP/$f"; then
        echo "   already patched: $f"
    elif same "$WORK/$k.from" "$APP/$f"; then
        TODO+=("$f")
    else
        die "$APP/$f is neither the $FROM nor the $TO version (changed by hand, or another release) — nothing was changed"
    fi
done
if [ ${#TODO[@]} -eq 0 ]; then
    say "nothing to do — this device already has the patch"
    exit 0
fi

BK="$BACKUPS/ceravis-patch-$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$BK"
say "backing up ${#TODO[@]} file(s) to $BK"
{
    echo "#!/usr/bin/env bash"
    echo "# Undo the patch $FROM -> $TO: put the old files back and restart."
    echo "set -euo pipefail"
} | sudo tee "$BK/ROLLBACK.sh" >/dev/null
for f in "${TODO[@]}"; do
    k="$(key "$f")"
    sudo cp -p "$APP/$f" "$BK/$k"
    printf 'cp -p %q %q\n' "$BK/$k" "$APP/$f" | sudo tee -a "$BK/ROLLBACK.sh" >/dev/null
done
printf 'systemctl restart %q\necho "rolled back"\n' "$SERVICE" | sudo tee -a "$BK/ROLLBACK.sh" >/dev/null
printf '%s -> %s\n%s\n' "$FROM" "$TO" "${TODO[*]}" | sudo tee "$BK/PATCH" >/dev/null

# Each file: written beside the old one with the old one's owner and mode, then
# renamed over it — a reader sees the old file or the new one, never half.
for f in "${TODO[@]}"; do
    k="$(key "$f")"
    tmp="$APP/$f.patch-new"
    sudo cp "$WORK/$k.to" "$tmp"
    sudo chown --reference="$APP/$f" "$tmp"
    sudo chmod --reference="$APP/$f" "$tmp"
    sudo mv -f "$tmp" "$APP/$f"
    echo "   patched: $f"
done

say "restarting $SERVICE"
sudo systemctl restart "$SERVICE"
until_s=$((SECONDS + HEALTH_SECS))
while [ "$SECONDS" -lt "$until_s" ]; do
    if curl -fs --max-time 3 "$HEALTH" >/dev/null 2>&1; then
        say "OK — $SERVICE is up with the patch ($FROM -> $TO)"
        echo "   undo, if ever needed:  sudo bash $BK/ROLLBACK.sh"
        exit 0
    fi
    sleep 2
done
echo "WARNING: $SERVICE does not answer $HEALTH after ${HEALTH_SECS}s." >&2
echo "   look:  sudo journalctl -u $SERVICE -n 40 --no-pager" >&2
echo "   undo:  sudo bash $BK/ROLLBACK.sh" >&2
exit 2
