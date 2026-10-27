#!/usr/bin/env bash
# =============================================================
# Send the release folder (dist/CERAVIS, from setup/make_release.sh) to a device
# over the network and install it — no monitor, no USB stick:
#
#     bash setup/send_to_device.sh ceravis@<device-ip>
#
#   1. this PC's admin SSH key is added to the device's admin account, so the
#      next steps (and every later login) use it
#   2. the folder is copied to ~/CERAVIS on the device, with the secrets and the
#      developers' SSH keys from ~/.ceravis/ (never from the project folder)
#   3. the install starts on the device as a background service and its
#      progress is shown here. A dropped connection or a closed window does NOT
#      stop it — run the same command with --follow to watch it again.
#   4. when it succeeds, the device deletes ~/CERAVIS, keys included
#
# The device's admin password is asked for by ssh/sudo themselves (typed by
# you, never stored). The device address: the router's client list, the name
# printed by the device, or 192.168.55.1 over the USB-C cable.
#
# Options:
#   --key FILE   the admin SSH key to use (default ~/.ssh/ceravis_admin, if it
#                exists; otherwise ssh's own default keys)
#   --follow     only show the progress of an install already running
#   --status     only say whether the last install finished, failed or runs
#   --no-install copy the folder, do not start the install
#   --code [REF] TESTING: send only the committed code of REF (default
#                prod_ceravis1.1) — a few MB — into ~/CERAVIS/patch on the
#                device, with the newest folder installer, and install again.
#                The release already there (models, packages) is reused, so a
#                fix travels in seconds. The delivered folder, patch included,
#                is deleted after a successful install as always.
# =============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PC="${CERAVIS_PC_DIR:-$HOME/.ceravis}"
OUT="$REPO/dist/CERAVIS"
TARGET=""; KEY=""; MODE=install; REF=prod_ceravis1.1
while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="$2"; shift 2 ;;
        --follow) MODE=follow; shift ;;
        --status) MODE=status; shift ;;
        --no-install) MODE=copy; shift ;;
        --code) MODE=code
                if [ $# -gt 1 ] && [[ "$2" != -* && "$2" != *@* ]]; then REF="$2"; shift; fi
                shift ;;
        -h|--help) sed -n '2,35p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
        *) TARGET="$1"; shift ;;
    esac
done
[ -n "$TARGET" ] || { echo "usage: bash setup/send_to_device.sh <admin>@<device-ip>" >&2; exit 2; }
[ -n "$KEY" ] || { [ -f "$HOME/.ssh/ceravis_admin" ] && KEY="$HOME/.ssh/ceravis_admin"; } || true
SSH_OPTS=(-o ServerAliveInterval=15 -o ServerAliveCountMax=4)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")
on() { ssh "${SSH_OPTS[@]}" -t "$TARGET" "$@"; }

case "$MODE" in
    follow) on "sudo bash ~/CERAVIS/install.sh --follow || sudo tail -n 60 /var/log/ceravis-install.log"; exit ;;
    status) on "sudo cat /var/log/ceravis-install.result 2>/dev/null || echo 'no install has run yet'"; exit ;;
    code)
        git -C "$REPO" rev-parse -q --verify "$REF^{commit}" >/dev/null \
            || { echo "no such branch or commit: $REF" >&2; exit 2; }
        STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
        mkdir -p "$STAGE/patch"
        git -C "$REPO" archive --worktree-attributes --format=tar "$REF" edge setup cloud \
            | tar -x -C "$STAGE/patch"
        git -C "$REPO" show "$REF:setup/usb/install.sh" > "$STAGE/install.sh"
        echo "== the code of $REF ($(git -C "$REPO" rev-parse --short "$REF"), $(du -sh "$STAGE/patch" | cut -f1)) -> ~/CERAVIS/patch"
        # One connection: the release must already be there; the old patch goes.
        tar -C "$STAGE" -cf - patch install.sh | ssh "${SSH_OPTS[@]}" "$TARGET" \
            'ls ~/CERAVIS/ceravis-edge-*.tar.gz >/dev/null 2>&1 || { echo "no release in ~/CERAVIS on the device: send the full release first (without --code)" >&2; exit 3; }
             rm -rf ~/CERAVIS/patch && tar -xf - -C ~/CERAVIS'
        echo "== installing again (the device keeps going if this window closes)"
        on "sudo bash ~/CERAVIS/install.sh"
        exit ;;
esac

ls "$OUT"/ceravis-edge-*.tar.gz >/dev/null 2>&1 \
    || { echo "no release in $OUT — build it first: bash setup/make_release.sh" >&2; exit 1; }
[ -s "$PC/ceravis-secrets.env" ] \
    || echo "note: no $PC/ceravis-secrets.env — the device uses the keys it already has"

echo "== 1/3 the admin SSH key on the device"
if [ -n "$KEY" ] && [ -f "$KEY.pub" ]; then
    ssh "${SSH_OPTS[@]}" "$TARGET" 'umask 077; mkdir -p ~/.ssh; k="$(cat)"; grep -qxF "$k" ~/.ssh/authorized_keys 2>/dev/null || echo "$k" >> ~/.ssh/authorized_keys' \
        < "$KEY.pub"
else
    echo "   (no --key / ~/.ssh/ceravis_admin — using ssh's default keys)"
fi

echo "== 2/3 copying the release folder to ~/CERAVIS"
ssh "${SSH_OPTS[@]}" "$TARGET" 'rm -rf ~/CERAVIS && mkdir -p ~/CERAVIS && chmod 700 ~/CERAVIS'
EXTRA=()
for f in ceravis-secrets.env authorized_keys; do [ -s "$PC/$f" ] && EXTRA+=("$PC/$f"); done
scp "${SSH_OPTS[@]}" -q "$OUT"/* ${EXTRA[@]+"${EXTRA[@]}"} "$TARGET:CERAVIS/"
echo "   copied ($(du -sh "$OUT" | cut -f1)$([ ${#EXTRA[@]} -gt 0 ] && echo ", with the secrets and SSH keys"))"

[ "$MODE" = copy ] && { echo "Install on the device:  sudo bash ~/CERAVIS/install.sh"; exit 0; }
echo "== 3/3 installing (the device keeps going if this window closes)"
on "sudo bash ~/CERAVIS/install.sh"
