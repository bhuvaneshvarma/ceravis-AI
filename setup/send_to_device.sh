#!/usr/bin/env bash
# =============================================================
# Send the release folder (dist/CERAVIS, from setup/make_release.sh) to a device
# over the network and install it — no monitor, no USB stick:
#
#     bash setup/send_to_device.sh ceravis@<device-ip>
#
#   1. the small files — the folder installer, the release's fingerprint, the
#      README, the secrets and the developers' SSH keys from ~/.ceravis/ (never
#      from the project folder) — go to ~/CERAVIS on the device, and this PC's
#      admin SSH key is added to the device's admin account: one connection
#   2. the release itself is streamed, with its progress. A link that drops
#      (the USB-C cable on Windows does) costs seconds, not the copy: this
#      waits for the device and RESUMES from the byte the device has. A partial
#      copy of the same build is resumed by the next run too; another build's
#      is replaced. The device checks the fingerprint before installing.
#   3. the install starts on the device as a background service and its
#      progress is shown here. A dropped connection or a closed window does NOT
#      stop it — --follow watches it again; --start starts it when the
#      connection closed before it began.
#   4. when it succeeds, the device deletes ~/CERAVIS, keys included
#
# The device's admin password is asked for by ssh/sudo themselves (typed by
# you, never stored) — once per connection: its SSH needs key AND password.
# The device address: the router's client list, the name printed by the
# device, or 192.168.55.1 over the USB-C cable.
#
# Options:
#   --key FILE   the admin SSH key to use (default ~/.ssh/ceravis_admin, if it
#                exists; otherwise ssh's own default keys)
#   --follow     only show the progress of an install already running
#   --status     only say whether the last install finished, failed or runs
#   --no-install copy the folder, do not start the install
#   --start      start the install of the folder already on the device (after a
#                connection that closed at step 3); shows it if it already runs
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
OUT="${CERAVIS_DIST:-$REPO/dist}/CERAVIS"
MAX_TRIES=20
TARGET=""; KEY=""; MODE=install; REF=prod_ceravis1.1
while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="$2"; shift 2 ;;
        --follow) MODE=follow; shift ;;
        --status) MODE=status; shift ;;
        --no-install) MODE=copy; shift ;;
        --start) MODE=start; shift ;;
        --code) MODE=code
                if [ $# -gt 1 ] && [[ "$2" != -* && "$2" != *@* ]]; then REF="$2"; shift; fi
                shift ;;
        -h|--help) sed -n '2,/^# =====/p' "${BASH_SOURCE[0]}" | sed '$d'; exit 0 ;;
        -*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
        *) TARGET="$1"; shift ;;
    esac
done
[ -n "$TARGET" ] || { echo "usage: bash setup/send_to_device.sh <admin>@<device-ip>" >&2; exit 2; }
[ -n "$KEY" ] || { [ -f "$HOME/.ssh/ceravis_admin" ] && KEY="$HOME/.ssh/ceravis_admin"; } || true
# A dead link is noticed within ~15 s, a device that does not answer in 10 s.
SSH_OPTS=(-o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")
HOST="${TARGET#*@}"
on() { ssh "${SSH_OPTS[@]}" -t "$TARGET" "$@"; }

# The USB-C cable on Windows: its old RNDIS network driver resets the link
# every few minutes, and every session in it dies; Windows' NCM driver for the
# same cable holds. Only ever advice, and only when the NCM adapter exists —
# disabling RNDIS without it would cut the cable link altogether.
usb_link_advice() {
    [ "$HOST" = 192.168.55.1 ] && command -v powershell.exe >/dev/null 2>&1 || return 0
    local found rndis ncm
    echo "== checking this PC's USB-C network driver (a few seconds)"
    # </dev/null: started from Git Bash, PowerShell otherwise waits for input.
    found="$(timeout 30 powershell.exe -NoProfile -NonInteractive -Command \
        "\$r = (Get-NetAdapter | Where-Object { \$_.InterfaceDescription -like '*Remote NDIS*' -and \$_.Status -eq 'Up' }).Name; \$n = (Get-NetAdapter | Where-Object { \$_.InterfaceDescription -like '*UsbNcm*' }).Name; \"\$r|\$n\"" \
        </dev/null 2>/dev/null | tr -d '\r')" || return 0
    rndis="${found%%|*}"; ncm="${found#*|}"
    [ -n "$rndis" ] && [ -n "$ncm" ] || return 0
    echo "note: the USB-C link runs on Windows' RNDIS driver ('$rndis'), which drops every few"
    echo "      minutes. Steadier: its NCM adapter ('$ncm'). In PowerShell as Administrator:"
    echo "        Enable-NetAdapter -Name '$ncm' -Confirm:\$false; Disable-NetAdapter -Name '$rndis' -Confirm:\$false"
    echo "      (the copy below resumes after any drop either way)"
}

# Until the device's SSH port answers again — the link is back (3 minutes).
wait_for_device() {
    local i
    for i in $(seq 60); do
        timeout 3 bash -c "exec 3<>/dev/tcp/$HOST/22" 2>/dev/null && return 0
        [ "$i" = 1 ] && echo "   waiting for the device to answer again…"
        sleep 3
    done
    echo "the device did not answer for 3 minutes — check the cable or network, then run the same command again (the copy resumes)" >&2
    return 1
}

# "$@" until it succeeds, waiting for the link between tries.
again() {
    local try
    for try in $(seq "$MAX_TRIES"); do
        "$@" && return 0
        [ "$try" -lt "$MAX_TRIES" ] || break
        echo "   the connection dropped — again (${try}/${MAX_TRIES})"
        wait_for_device || return 1
    done
    echo "gave up after $MAX_TRIES tries — run the same command again: the copy resumes where it stopped" >&2
    return 1
}
usb_link_advice

case "$MODE" in
    follow) on "sudo bash ~/CERAVIS/install.sh --follow || sudo tail -n 60 /var/log/ceravis-install.log"; exit ;;
    status) on "sudo cat /var/log/ceravis-install.result 2>/dev/null || echo 'no install has run yet'"; exit ;;
    start) on "sudo bash ~/CERAVIS/install.sh"; exit ;;
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

NAME="$(cd "$OUT" && ls ceravis-edge-*.tar.gz | head -1)"
BUNDLE="$OUT/$NAME"
SIZE="$(wc -c < "$BUNDLE" | tr -d ' ')"

# Step 1 on the device, from one tar stream: the small files in place, every
# *.pub in it added to the admin account, and a release file already there
# kept only when its fingerprint is this build's (then the copy resumes).
# Answers "<bytes of the release there>", or BUSY while an install runs (its
# end deletes this folder).
PREPARE='set -e; umask 077
if systemctl is-active --quiet ceravis-install 2>/dev/null; then echo BUSY; exit 0; fi
mkdir -p ~/CERAVIS ~/.ssh; chmod 700 ~/CERAVIS; cd ~/CERAVIS
rm -rf .new; mkdir .new; tar -xf - -C .new
for p in .new/*.pub; do
    [ -f "$p" ] || continue
    k="$(cat "$p")"; rm -f "$p"
    grep -qxF "$k" ~/.ssh/authorized_keys 2>/dev/null || echo "$k" >> ~/.ssh/authorized_keys
done
s="$(cd .new && echo ceravis-edge-*.tar.gz.sha256)"; b="${s%.sha256}"
keep=.new; [ -f "$b" ] && cmp -s "$s" ".new/$s" && keep="$b"
find . -mindepth 1 -maxdepth 1 ! -name .new ! -name "$keep" -exec rm -rf {} +
find .new -mindepth 1 -maxdepth 1 -exec mv -f {} . \;
rmdir .new
stat -c %s "$b" 2>/dev/null || echo 0'

SMALL=(-C "$OUT" install.sh README.txt "$NAME.sha256")
WITH=()
for f in ceravis-secrets.env authorized_keys; do [ -s "$PC/$f" ] && WITH+=("$f"); done
[ ${#WITH[@]} -gt 0 ] && SMALL+=(-C "$PC" "${WITH[@]}")
if [ -n "$KEY" ] && [ -f "$KEY.pub" ]; then
    SMALL+=(-C "$(dirname "$KEY")" "$(basename "$KEY").pub")
else
    echo "note: no --key / ~/.ssh/ceravis_admin — ssh's default keys are used"
fi

HAVE=0
prepare() {
    local out
    out="$(tar -cf - "${SMALL[@]}" | ssh "${SSH_OPTS[@]}" "$TARGET" "$PREPARE")" || return 1
    if [ "$out" = BUSY ]; then
        echo "an install is running on the device — wait for it to end (--follow), then send again" >&2
        exit 3
    fi
    [[ "$out" =~ ^[0-9]+$ ]] || { echo "unexpected answer from the device: $out" >&2; return 1; }
    HAVE="$out"
}
size_there() {
    local out
    out="$(ssh "${SSH_OPTS[@]}" "$TARGET" "stat -c %s ~/CERAVIS/$NAME 2>/dev/null || echo 0")" || return 1
    [[ "$out" =~ ^[0-9]+$ ]] && HAVE="$out"
}
# From the byte the device has; from the start (replacing it) when it holds
# more than this release — then it is not this release.
send_rest() {
    local into='>>'
    [ "$HAVE" -le "$SIZE" ] || HAVE=0
    [ "$HAVE" -gt 0 ] || into='>'
    [ "$HAVE" -eq 0 ] || echo "   resuming at $((HAVE / 1048576)) of $((SIZE / 1048576)) MB"
    dd if="$BUNDLE" bs=1M iflag=skip_bytes skip="$HAVE" status=progress \
        | ssh "${SSH_OPTS[@]}" "$TARGET" "cat $into ~/CERAVIS/$NAME"
}
copy_release() {
    local try
    for try in $(seq "$MAX_TRIES"); do
        [ "$HAVE" != "$SIZE" ] || return 0
        send_rest && return 0
        [ "$try" -lt "$MAX_TRIES" ] || break
        echo "   the link dropped — the copy resumes (${try}/${MAX_TRIES})"
        wait_for_device && again size_there || return 1
    done
    echo "gave up after $MAX_TRIES tries — run the same command again: the copy resumes where it stopped" >&2
    return 1
}

echo "== 1/3 the small files, the secrets and the admin SSH key → ~/CERAVIS"
again prepare
echo "== 2/3 the release ($((SIZE / 1048576)) MB) → ~/CERAVIS/$NAME"
if [ "$HAVE" = "$SIZE" ]; then
    echo "   already on the device (this same build) — not sent again"
else
    copy_release
fi
echo "   copied$([ ${#WITH[@]} -gt 0 ] && echo ", with the secrets and SSH keys")"

[ "$MODE" = copy ] && { echo "Install on the device:  sudo bash ~/CERAVIS/install.sh"; exit 0; }
echo "== 3/3 installing (the device keeps going if this window closes)"
on "sudo bash ~/CERAVIS/install.sh" || {
    rc=$?
    # 255: the connection itself — e.g. a password not typed within the 30 s
    # the device's SSH allows. The release is there; nothing is copied again.
    [ "$rc" = 255 ] && echo "the connection closed — start (or watch) the install:  bash setup/send_to_device.sh $TARGET --start" >&2
    exit "$rc"
}
