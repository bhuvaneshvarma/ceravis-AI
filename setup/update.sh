#!/usr/bin/env bash
# =============================================================
# CERAVIS update — install a new release beside the running one, switch to it,
# check it, and switch BACK by itself if it is not healthy.
#
#   1. fetch     the bundle (a file, or an https URL)
#   2. verify    its SHA-256 (from --sha256, or the .sha256 file next to it)
#   3. stage     unpack into /opt/ceravis/releases/<version> — the running
#                release is not touched
#   4. build     its own virtualenv (the cached pycuda wheel makes this quick)
#                and its AI engines, for this device: the running release's
#                engine is taken only when its fingerprint proves it identical
#                (same ONNX, same TensorRT, same board), else it is built
#   5. switch    /opt/ceravis/current -> the new release, restart the services
#   6. check     the API answers /health and the media server is up, within
#                CHECK_SECS (default 240)
#   7. keep      or ROLL BACK: point current at the previous release again,
#                restart, and report failure. The data folder is shared, so a
#                rollback loses nothing.
#
# The last KEEP (default 3) releases stay on disk; older ones are removed.
# Every outcome is appended to /var/lib/ceravis/update_history.jsonl.
#
# Run:  sudo bash /opt/ceravis/current/setup/update.sh <bundle.tar.gz | https://…> [--sha256 HEX]
#       sudo bash /opt/ceravis/current/setup/update.sh --rollback     (back to the previous release)
# =============================================================
set -euo pipefail

APP=/opt/ceravis
DATA=/var/lib/ceravis
CHECK_SECS="${CHECK_SECS:-240}"
KEEP="${KEEP:-3}"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0 …" >&2; exit 1; }
[ -f /etc/ceravis/layout.env ] || { echo "not a production install (no /etc/ceravis/layout.env)" >&2; exit 1; }
# One install or update at a time (the same lock as setup/install.sh).
exec 9>/run/ceravis-install.lock
flock -n 9 || { echo "another CERAVIS install or update is running" >&2; exit 1; }

SOURCE=""; SHA=""; ROLLBACK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --sha256) SHA="$2"; shift 2 ;;
        --rollback) ROLLBACK=1; shift ;;
        -h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) SOURCE="$1"; shift ;;
    esac
done

CURRENT="$(readlink -f "$APP/current")"
OLD_VERSION="$(basename "$CURRENT")"
log() { echo "update: $*"; }
history() {     # <result> <version> <detail>
    printf '{"at":"%s","result":"%s","from":"%s","to":"%s","detail":"%s"}\n' \
        "$(date -Is)" "$1" "$OLD_VERSION" "$2" "${3//\"/\'}" >> "$DATA/update_history.jsonl"
}
switch_to() {   # <release dir>
    ln -sfn "$1" "$APP/current.new" && mv -T "$APP/current.new" "$APP/current"
    systemctl daemon-reload
    systemctl restart ceravis
    systemctl restart ceravis-fleet-agent 2>/dev/null || true
}
healthy() {
    local until=$((SECONDS + CHECK_SECS)) body
    while [ "$SECONDS" -lt "$until" ]; do
        if body="$(curl -fs --max-time 5 http://127.0.0.1:8000/health 2>/dev/null)" \
           && echo "$body" | grep -q '"status": *"ok"' \
           && echo "$body" | grep -q '"media_backbone": *true'; then
            return 0
        fi
        sleep 5
    done
    return 1
}

# ---- rollback on request ---------------------------------------------------
if [ "$ROLLBACK" = 1 ]; then
    PREV="$(ls -1dt "$APP"/releases/*/ | grep -v "^$CURRENT/\?$" | grep -v '\.failed/$' | head -1)" || PREV=""
    [ -n "$PREV" ] || { log "no previous release to go back to"; exit 1; }
    PREV="${PREV%/}"
    log "rolling back $OLD_VERSION -> $(basename "$PREV")"
    switch_to "$PREV"
    if healthy; then history rolled-back "$(basename "$PREV")" "on request"; log "done"; exit 0; fi
    history rollback-unhealthy "$(basename "$PREV")" "previous release also unhealthy"
    log "the previous release is not healthy either — see journalctl -u ceravis"; exit 1
fi

[ -n "$SOURCE" ] || { echo "usage: sudo bash $0 <bundle.tar.gz | https://…> [--sha256 HEX]" >&2; exit 2; }

# ---- 1-2. fetch + verify ---------------------------------------------------------
WORK="$(mktemp -d /var/tmp/ceravis-update.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
case "$SOURCE" in
    https://*)
        curl -fL --retry 3 -o "$WORK/bundle.tar.gz" "$SOURCE"
        [ -n "$SHA" ] || SHA="$(curl -fsL "$SOURCE.sha256" 2>/dev/null | awk '{print $1}')" ;;
    http://*) log "refusing plain http — use https"; exit 2 ;;
    *)  cp "$SOURCE" "$WORK/bundle.tar.gz"
        [ -n "$SHA" ] || { [ -f "$SOURCE.sha256" ] && SHA="$(awk '{print $1}' "$SOURCE.sha256")"; } ;;
esac
[ -n "$SHA" ] || { log "no SHA-256 to check the bundle against (give --sha256)"; exit 2; }
echo "$SHA  $WORK/bundle.tar.gz" | sha256sum -c --quiet - || { log "SHA-256 mismatch — bundle rejected"; exit 3; }
log "bundle verified"

# ---- 3. stage -----------------------------------------------------------------------
mkdir "$WORK/x" && tar -xzf "$WORK/bundle.tar.gz" -C "$WORK/x"
TOP="$(find "$WORK/x" -mindepth 1 -maxdepth 1 -type d | head -1)" || TOP=""
[ -f "$TOP/edge/VERSION" ] || { log "not a CERAVIS bundle (no edge/VERSION)"; exit 3; }
NEW_VERSION="$(tr -d '[:space:]' < "$TOP/edge/VERSION")"
NEW="$APP/releases/$NEW_VERSION"
[ "$NEW" != "$CURRENT" ] || { log "$NEW_VERSION is already running"; exit 0; }
rm -rf "$NEW" "$NEW.failed"
mv "$TOP" "$NEW"
log "staged $NEW_VERSION (running: $OLD_VERSION)"

# ---- 4. build: software + engines --------------------------------------------------------
# The layout file (lib.sh) with the new release's own folders in front.
export CERAVIS_ROOT="$NEW" CERAVIS_PYTHON="$NEW/venv/bin/python" CERAVIS_MODELS_DIR="$NEW/models"
# shellcheck source=setup/lib.sh
. "$NEW/setup/lib.sh"
export_layout
build() {
    CERAVIS_VENV="$NEW/venv" CERAVIS_WHEELHOUSE="$APP/wheels" CERAVIS_SKIP_APT=1 \
        bash "$NEW/setup/install_native.sh" >"$WORK/build.log" 2>&1 || return 1
    "$NEW/venv/bin/python" -m compileall -q "$NEW/edge" >/dev/null 2>&1 || true
    install -d -m 0755 -o "$CERAVIS_USER" -g "$CERAVIS_USER" "$NEW/models"
    # The ONNX models come with the bundle; one it lacks is taken from the
    # running release — an ONNX only, never an engine.
    if [ -d "$NEW/models-src" ]; then
        cp -a "$NEW/models-src/." "$NEW/models/" && rm -rf "$NEW/models-src"
    fi
    (cd "$CURRENT/models" 2>/dev/null && find . -type f -name '*.onnx') | while IFS= read -r f; do
        [ -e "$NEW/models/$f" ] || install -D -m 0644 "$CURRENT/models/$f" "$NEW/models/$f"
    done
    # Engines are built for this device; the running release's engine is taken
    # only when its fingerprint proves it identical (same ONNX, TensorRT, board).
    "$NEW/venv/bin/python" "$NEW/setup/export_models.py" --reuse-from "$CURRENT/models" \
        >>"$WORK/build.log" 2>&1 || return 1
    lock_release "$NEW"
}
if ! build; then
    mv "$NEW" "$NEW.failed"
    history build-failed "$NEW_VERSION" "$(tail -3 "$WORK/build.log" | tr '\n' ' ')"
    log "build failed — still running $OLD_VERSION. Log:"; tail -20 "$WORK/build.log"
    exit 4
fi
log "built"

# ---- 5. switch -----------------------------------------------------------------------------
# Units follow the switching path (the layout file's own values); re-written so
# a changed template applies.
unset "${LAYOUT_KEYS[@]}"
. "$NEW/setup/lib.sh"
for u in ceravis.service ceravis-reboot.service ceravis-reboot.timer \
         ceravis-fleet-agent.service ceravis-firstboot.service; do
    install_unit "$NEW/edge/infra/systemd/$u"
done
install_unit "$NEW/cloud/systemd/frpc.service"
log "switching to $NEW_VERSION"
switch_to "$NEW"

# ---- 6-7. check, keep or roll back -----------------------------------------------------------
if healthy; then
    history ok "$NEW_VERSION" ""
    log "$NEW_VERSION is healthy — kept"
    # Keep the newest $KEEP releases (never the running one).
    ls -1dt "$APP"/releases/*/ | sed 's|/$||' | grep -vx "$NEW" | tail -n +"$KEEP" \
        | while IFS= read -r old; do rm -rf "$old"; log "removed old release $(basename "$old")"; done
    exit 0
fi
log "$NEW_VERSION is NOT healthy within ${CHECK_SECS}s — rolling back to $OLD_VERSION"
journalctl -u ceravis -n 30 --no-pager > "$WORK/fail.log" 2>&1 || true
for u in ceravis.service ceravis-reboot.service ceravis-reboot.timer \
         ceravis-fleet-agent.service ceravis-firstboot.service; do
    install_unit "$CURRENT/edge/infra/systemd/$u"
done
install_unit "$CURRENT/cloud/systemd/frpc.service"
switch_to "$CURRENT"
mv "$NEW" "$NEW.failed"
if healthy; then
    history rolled-back "$NEW_VERSION" "$(tail -3 "$WORK/fail.log" | tr '\n' ' ')"
    log "rolled back — $OLD_VERSION is running again"
else
    history rollback-unhealthy "$NEW_VERSION" "previous release unhealthy after rollback"
    log "rolled back, but $OLD_VERSION is not healthy either — see journalctl -u ceravis"
fi
tail -15 "$WORK/fail.log"
exit 5
