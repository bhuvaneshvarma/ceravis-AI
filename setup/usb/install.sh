#!/usr/bin/env bash
# =============================================================
# CERAVIS — install the release in this folder. The ONE command, from a USB
# stick or from a copy of this folder on the device (setup/send_to_device.sh):
#
#     sudo bash <this folder>/install.sh
#
# It checks the release against its fingerprint (.sha256), then installs it as
# a background service (ceravis-install): a closed terminal, a dropped SSH or
# Wi-Fi connection does not stop it. Meanwhile it shows the progress — Ctrl+C
# only stops the showing.
#
#     sudo bash <this folder>/install.sh --follow   show the progress again
#     sudo bash <this folder>/install.sh --status   running / finished / failed
#     sudo tail -f /var/log/ceravis-install.log     the whole log, any time
#
# Uses ceravis-secrets.env and authorized_keys (developers' SSH public keys)
# from this folder when they are here. When the install succeeds and this
# folder sits on the device's own disk (a copy, not a USB stick), the folder —
# keys included — is deleted: no secret or code stays in a home folder.
# Extra options are passed on to the installer (setup/install.sh --help).
# =============================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
[ "$(id -u)" = 0 ] || exec sudo bash "$0" "$@"

UNIT=ceravis-install
LOG=/var/log/ceravis-install.log
RESULT=/var/log/ceravis-install.result
WORK=/var/tmp/ceravis-install

say() { echo "== $*"; }

status() {
    if systemctl is-active --quiet "$UNIT"; then
        say "installing now — follow it:  sudo bash $HERE/install.sh --follow"
    elif [ -s "$RESULT" ]; then
        say "last install: $(cat "$RESULT")   (log: $LOG)"
    else
        say "no install has run on this device yet"
    fi
}

follow() {
    say "following the install (Ctrl+C stops only the showing — it keeps running)"
    tail -n 40 -F "$LOG" 2>/dev/null &
    local tailer=$!
    trap 'kill "$tailer" 2>/dev/null; echo; status; exit 0' INT
    while systemctl is-active --quiet "$UNIT"; do sleep 2; done
    sleep 1; kill "$tailer" 2>/dev/null || true
    echo; status
    grep -q '^ok' "$RESULT" 2>/dev/null
}

# A copy on the device's own disk (scp), not a USB stick or other removable
# drive (mounted under /media, /mnt or /run/media; FAT/exFAT/NTFS).
on_device_disk() {
    case "$HERE" in /media/*|/mnt/*|/run/media/*) return 1 ;; esac
    case "$(findmnt -n -o FSTYPE -T "$HERE" 2>/dev/null)" in
        vfat|exfat|ntfs|ntfs3|fuseblk) return 1 ;;
    esac
    return 0
}

# What the background service runs: unpack, install, record the result, tidy.
run() {
    local bundle top rc=0 args=()
    trap 'echo "failed $(date -Is)" > "$RESULT"' ERR
    bundle="$(ls -1t "$HERE"/ceravis-edge-*.tar.gz | head -1)"
    rm -rf "$WORK" && mkdir -p "$WORK"
    tar -xzf "$bundle" -C "$WORK"
    top="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -1)"
    [ -f "$HERE/ceravis-secrets.env" ] && args+=(--secrets "$HERE/ceravis-secrets.env")
    [ -f "$HERE/authorized_keys" ] && args+=(--admin-keys "$HERE/authorized_keys")
    bash "$top/install.sh" ${args[@]+"${args[@]}"} "$@" || rc=$?
    rm -rf "$WORK"
    if [ "$rc" != 0 ]; then
        echo "failed (exit $rc) $(date -Is)" > "$RESULT"
        echo "INSTALL FAILED (exit $rc) — fix what the log says and run the same command again." | tee -a "$LOG"
        return "$rc"
    fi
    echo "ok $(date -Is)" > "$RESULT"
    # A copy on the device's own disk is not needed any more: the keys are in
    # root-only files, the code is in /opt/ceravis. A USB stick is left as it is.
    if on_device_disk; then
        [ -f "$HERE/ceravis-secrets.env" ] && shred -u "$HERE/ceravis-secrets.env" 2>/dev/null
        rm -rf "$HERE"
        echo "the delivered folder $HERE (and the keys in it) deleted" | tee -a "$LOG"
    fi
}

case "${1:-}" in
    --status) status; exit 0 ;;
    --follow) follow; exit $? ;;
    --run) shift; run "$@"; exit $? ;;
esac

if systemctl is-active --quiet "$UNIT"; then
    say "an install is already running"
    follow; exit $?
fi

# Checked here, in the foreground, so a damaged copy is said at once.
BUNDLE="$(ls -1t "$HERE"/ceravis-edge-*.tar.gz 2>/dev/null | head -1)"
[ -n "$BUNDLE" ] || { echo "no ceravis-edge-*.tar.gz in $HERE" >&2; exit 1; }
say "checking $(basename "$BUNDLE")"
(cd "$HERE" && sha256sum -c --quiet "$(basename "$BUNDLE").sha256") \
    || { echo "the release file is damaged or was changed — copy the folder again" >&2; exit 1; }
[ -f "$HERE/ceravis-secrets.env" ] || say "no ceravis-secrets.env here — the installer uses the keys already on the device"

rm -f "$RESULT"
ADMIN_ARGS=()
[ -n "${SUDO_USER:-}" ] && ADMIN_ARGS=(--admin "$SUDO_USER")
systemd-run --unit="$UNIT" --collect --description="CERAVIS install" \
    --property=KillMode=mixed --property=TimeoutStopSec=60 \
    /bin/bash "$HERE/install.sh" --run ${ADMIN_ARGS[@]+"${ADMIN_ARGS[@]}"} "$@" >/dev/null
say "install started in the background (service $UNIT)"
follow
