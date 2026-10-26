#!/usr/bin/env bash
# =============================================================
# CERAVIS — ONE-COMMAND PRODUCTION INSTALL (Jetson Orin Nano, JetPack 6)
# =============================================================
# Takes a device with JetPack installed (SDK Manager) to a running CERAVIS
# edge, ready for its first sign-in at  https://<device-ip>:8443
#
#   [1/9] check     the device, JetPack, disk space, internet, the secrets
#   [2/9] system    no desktop, power mode, fan, NVIDIA packages held, logs,
#                   background services off
#   [3/9] account   the ceravis-svc service account and its folders
#   [4/9] release   this version into /opt/ceravis/releases/<version>
#   [5/9] software  Python packages in the release's own virtualenv
#   [6/9] AI models TensorRT engines built for this GPU (the ONNX come with
#                   the release — no internet or torch needed for them)
#   [7/9] secrets   app-server key, fleet key, tunnel, reboot password — root-
#                   only files; SSH needs a developer's key AND the password
#   [8/9] identity  device name, camera hotspot, keys (first boot)
#   [9/9] services  start everything, then the full health check
#
# Production layout (setup/lib.sh, /etc/ceravis/layout.env):
#   /opt/ceravis/releases/<v>/   code + virtualenv + engines — root's, readable
#                                by the service only (not by login accounts)
#   /opt/ceravis/current         -> the release in use (switched by update.sh)
#   /var/lib/ceravis/            everything the device writes (data)
#   /etc/ceravis/credstore/      secrets, root-only, handed over as credentials
#
# Secrets come from ONE file you keep off the release (never in the repo):
#   CERAVIS_API_KEY=…  FRP_SERVER_ADDR=…  FRP_TOKEN=…        (required)
#   FMS_ENROLL_KEY=…   REBOOT_PASSWORD=…                     (recommended)
# given with --secrets FILE, else ceravis-secrets.env next to the release
# folder or in it. KEY=value lines: spaces around '=' and quotes are fine, the
# last line needs no newline. The install STOPS before changing anything when
# a required key is neither in the file nor already on the device (set
# ALLOW_MISSING_SECRETS=1 to install without them, e.g. on a test bench).
#
# Run:  sudo bash install.sh [--secrets FILE] [--admin-keys FILE] [--admin USER]
#                            [--power-mode 25W|MAXN_SUPER|15W] [--keep-desktop]
#       sudo bash /opt/ceravis/current/setup/install.sh --secrets-only --secrets FILE
#            (add or replace keys on an installed device; nothing else changes)
#
#   --admin-keys FILE  developers' SSH public keys for the admin account; with at
#                      least one key there, SSH asks for the key AND the password
#   --admin USER       that account (default: the one that ran sudo)
#
# Everything is logged to /var/log/ceravis-install.log; one install at a time.
# Re-running is safe: finished work is kept, the same version is refreshed.
# =============================================================
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"     # the release folder, real path
SECRETS=""; ADMIN_KEYS=""; ADMIN="${SUDO_USER:-}"
POWER="${POWER_MODE:-25W}"; HEADLESS=1; SECRETS_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --secrets) SECRETS="$2"; shift 2 ;;
        --secrets-only) SECRETS_ONLY=1; shift ;;
        --admin-keys) ADMIN_KEYS="$2"; shift 2 ;;
        --admin) ADMIN="$2"; shift 2 ;;
        --power-mode) POWER="$2"; shift 2 ;;
        --keep-desktop) HEADLESS=0; shift ;;
        -h|--help) sed -n '2,49p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "run as root:  sudo bash $0" >&2; exit 1; }

APP=/opt/ceravis
VERSION="$(tr -d '[:space:]' < "$SRC/edge/VERSION")"
REL="$APP/releases/$VERSION"
SVC=ceravis-svc
DATA=/var/lib/ceravis
CRED=/etc/ceravis/credstore
LOG=/var/log/ceravis-install.log

stage() { echo; echo "============ [$1] $2 ============"; }
fail()  { echo "INSTALL STOPPED: $*" >&2; exit 1; }
warn()  { echo "  WARNING: $*"; }

# One install (or update) at a time, and everything into the log as well.
exec 9>/run/ceravis-install.lock
flock -n 9 || fail "another CERAVIS install or update is running — see $LOG"
touch "$LOG" && chmod 0640 "$LOG"
exec > >(tee -a "$LOG") 2>&1
echo "==== $(date -Is)  CERAVIS $VERSION from $SRC$([ "$SECRETS_ONLY" = 1 ] && echo '  (secrets only)')"

# ---- the secrets file ---------------------------------------------------------
# Read as data, never executed: KEY=value lines, spaces around '=' allowed,
# quotes stripped, CRLF and a last line without a newline tolerated. Only these
# keys are taken. The summary names what was found — never a value.
SECRET_KEYS=(CERAVIS_API_KEY FRP_SERVER_ADDR FRP_TOKEN FMS_ENROLL_KEY REBOOT_PASSWORD)
read_secrets() {
    local line k v
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*([A-Z_]+)[[:space:]]*=[[:space:]]*(.*)$ ]] || continue
        k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
        v="${v%"${v##*[![:space:]]}"}"
        if [[ "$v" =~ ^\"(.*)\"$ ]] || [[ "$v" =~ ^\'(.*)\'$ ]]; then v="${BASH_REMATCH[1]}"; fi
        case " ${SECRET_KEYS[*]} " in
            *" $k "*) [ -n "$v" ] && export "$k=$v" ;;
        esac
    done < "$1"
}
tunnel_configured() {
    [ -f /etc/frp/frpc.toml ] && ! grep -Eq 'EC2_PUBLIC_IP_OR_DOMAIN|CHANGE_ME_TO_A' /etc/frp/frpc.toml
}
if [ -z "$SECRETS" ]; then
    for f in "$(dirname "$SRC")/ceravis-secrets.env" "$SRC/ceravis-secrets.env"; do
        [ -f "$f" ] && { SECRETS="$f"; break; }
    done
fi
if [ -n "$SECRETS" ]; then
    [ -f "$SECRETS" ] || fail "secrets file not found: $SECRETS"
    read_secrets "$SECRETS"
    echo "  secrets: $SECRETS"
else
    echo "  secrets: no file given"
fi
for k in "${SECRET_KEYS[@]}"; do
    if [ -n "${!k:-}" ]; then echo "    $k: found"; else echo "    $k: not in the file"; fi
done
MISSING=()
[ -n "${CERAVIS_API_KEY:-}" ] || [ -s "$CRED/ceravis_api_key" ] \
    || MISSING+=("CERAVIS_API_KEY — without it no alert reaches the app server")
{ [ -n "${FRP_SERVER_ADDR:-}" ] && [ -n "${FRP_TOKEN:-}" ]; } || tunnel_configured \
    || MISSING+=("FRP_SERVER_ADDR + FRP_TOKEN — without them nobody can reach this device remotely")
if [ "${#MISSING[@]}" -gt 0 ]; then
    printf '    missing: %s\n' "${MISSING[@]}"
    [ "${ALLOW_MISSING_SECRETS:-0}" = 1 ] \
        || fail "add the missing keys to the secrets file and run this again (nothing was changed)"
    warn "installing without them (ALLOW_MISSING_SECRETS=1)"
fi
[ -n "${FMS_ENROLL_KEY:-}" ] || [ -s /etc/ceravis-fleet-agent/enroll.env ] \
    || warn "no FMS_ENROLL_KEY — the device will not appear in the fleet console"

# What step 7 does, on a full install and with --secrets-only alike: each key
# into its own root-only file; the services that use one restarted with it.
apply_secrets() {
    bash "$CERAVIS_ROOT/setup/install_api_key.sh"
    bash "$CERAVIS_ROOT/cloud/install_frpc.sh"
    if [ -n "${REBOOT_PASSWORD:-}" ]; then
        printf '%s\n' "$REBOOT_PASSWORD" \
            | "$CERAVIS_PYTHON" "$CERAVIS_ROOT/setup/set_reboot_password.py" --stdin --replace
    elif [ ! -s "$(data_dir)/reboot_auth.json" ]; then
        warn "no REBOOT_PASSWORD — the manual reboot stays locked (the nightly one still runs)"
    fi
}

# SSH: once a developer key is installed for the admin account, SSH asks for
# the key AND the account's password (both). Without any key, nothing changes —
# a key-and-password rule with no key would lock everyone out.
setup_ssh() {
    local home conf=/etc/ssh/sshd_config.d/10-ceravis.conf
    [ -n "$ADMIN" ] || ADMIN="$(awk -F: '$3 >= 1000 && $3 < 60000 && $7 !~ /(nologin|false)$/ {print $1; exit}' /etc/passwd)"
    id -u "$ADMIN" >/dev/null 2>&1 || { warn "no admin account found — SSH left as it is"; return 0; }
    home="$(getent passwd "$ADMIN" | cut -d: -f6)"
    install -d -m 0700 -o "$ADMIN" -g "$(id -gn "$ADMIN")" "$home/.ssh"
    touch "$home/.ssh/authorized_keys"
    if [ -n "$ADMIN_KEYS" ] && [ -s "$ADMIN_KEYS" ]; then
        local key added=0
        while IFS= read -r key || [ -n "$key" ]; do
            key="${key%$'\r'}"
            [[ "$key" =~ ^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-) ]] || continue
            grep -qxF "$key" "$home/.ssh/authorized_keys" || { echo "$key" >> "$home/.ssh/authorized_keys"; added=$((added + 1)); }
        done < "$ADMIN_KEYS"
        echo "  SSH: $added developer key(s) added for $ADMIN"
    fi
    chown "$ADMIN:$(id -gn "$ADMIN")" "$home/.ssh/authorized_keys"
    chmod 0600 "$home/.ssh/authorized_keys"
    if ! grep -Eq '^(ssh-|ecdsa-|sk-)' "$home/.ssh/authorized_keys"; then
        warn "no developer SSH key for $ADMIN — SSH stays password-only (add one: --admin-keys FILE)"
        return 0
    fi
    cat > "$conf" <<EOF
# CERAVIS (setup/install.sh): SSH needs BOTH a developer's key AND the account
# password, for $ADMIN only. The console and the USB-C serial login still take
# the password alone, so a lost key never locks the device.
PermitRootLogin no
AllowUsers $ADMIN
PubkeyAuthentication yes
PasswordAuthentication yes
KbdInteractiveAuthentication no
AuthenticationMethods publickey,password
MaxAuthTries 4
LoginGraceTime 30
EOF
    chmod 0644 "$conf"
    if sshd -t 2>/dev/null; then
        systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
        echo "  SSH: key AND password required (for $ADMIN)"
    else
        rm -f "$conf"
        warn "the SSH rule did not validate — removed, SSH left as it was"
    fi
}

if [ "$SECRETS_ONLY" = 1 ]; then
    [ -L "$APP/current" ] || fail "no CERAVIS installed here yet — run the full install"
    # shellcheck source=setup/lib.sh
    . "$APP/current/setup/lib.sh"
    export_layout
    stage "secrets" "keys into their root-only files"
    apply_secrets
    [ -z "$ADMIN_KEYS" ] || setup_ssh
    # A rebuilt tunnel config is keyed to the account's edge_id again by the
    # app itself at start (its boot-time self-heal).
    if [ -n "${FRP_SERVER_ADDR:-}" ]; then systemctl try-restart ceravis || true; fi
    if [ -n "${FMS_ENROLL_KEY:-}" ]; then
        bash "$CERAVIS_ROOT/setup/install_fleet_agent.sh" </dev/null >/dev/null
    fi
    echo; echo "  Secrets updated. $(date -Is)"
    exit 0
fi

# ---- [1/9] check ----------------------------------------------------------
stage "1/9" "checking this device"
[ "$(uname -m)" = aarch64 ] || fail "this is not an arm64 Jetson ($(uname -m))"
grep -q 'R36' /etc/nv_tegra_release 2>/dev/null \
    || fail "JetPack 6 (L4T R36) not found — install it with SDK Manager first"
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[ "${FREE_GB:-0}" -ge 15 ] || fail "only ${FREE_GB} GB free on / — at least 15 GB needed"
if curl -fsI --max-time 8 https://pypi.org >/dev/null 2>&1; then
    echo "  internet: yes"
else
    echo "  internet: no — the Python and system packages the device still lacks"
    echo "            cannot be fetched. Connect an Ethernet cable from the router."
    [ -d "$SRC/wheels" ] || fail "no internet: connect the device to the router by Ethernet cable"
fi
echo "  CERAVIS $VERSION from $SRC, JetPack $(head -1 /etc/nv_tegra_release | grep -oE 'R36[^,]*, REVISION: [0-9.]+')"

# ---- [2/9] system -----------------------------------------------------------
stage "2/9" "system settings"
POWER_MODE="$POWER" HEADLESS="$HEADLESS" bash "$SRC/setup/tune_os.sh"

# ---- [3/9] account + folders --------------------------------------------------
stage "3/9" "service account and folders"
if ! id -u "$SVC" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin --user-group "$SVC"
    echo "  created $SVC (no login, no password)"
fi
# GPU / video decode devices are group "video" on Jetson; journal for the
# fleet console's "logs" order.
for g in video render systemd-journal; do
    getent group "$g" >/dev/null && usermod -a -G "$g" "$SVC"
done
install -d -m 0750 -o "$SVC" -g "$SVC" "$DATA"
install -d -m 0755 -o root -g root /etc/ceravis      # layout.env (no secret)
install -d -m 0700 -o root -g root "$CRED"           # the secrets
install -d -m 0755 "$APP"
install -d -m 0750 -o root -g "$SVC" "$APP/releases"

# ---- [4/9] release ------------------------------------------------------------
stage "4/9" "release $VERSION"
if [ -d "$REL" ] && [ "$SRC" = "$(cd "$REL" && pwd -P)" ]; then
    echo "  running from the installed release itself — code kept as it is"
else
    mkdir -p "$REL"
    # Code only: a re-run keeps the release's virtualenv and built engines; the
    # ONNX models go straight into models/ below.
    tar -C "$SRC" --exclude=./.git --exclude=./dist --exclude=./venv --exclude=./models \
        --exclude=./models-src --exclude=./wheels -cf - . | tar -C "$REL" -xf -
fi
install -d -m 0755 -o "$SVC" -g "$SVC" "$REL/models"
if [ -d "$SRC/models-src" ]; then
    (cd "$SRC/models-src" && find . -type f -name '*.onnx') | while IFS= read -r f; do
        cmp -s "$SRC/models-src/$f" "$REL/models/$f" \
            || install -D -m 0644 -o "$SVC" -g "$SVC" "$SRC/models-src/$f" "$REL/models/$f"
    done
    echo "  ONNX models from the release in place"
fi

# The device's ONE layout file (setup/lib.sh reads it in every script, the app's
# settings too): the services and every later script use /opt/ceravis/current.
CERAVIS_ROOT="$APP/current" CERAVIS_USER="$SVC" CERAVIS_PYTHON="$APP/current/venv/bin/python" \
CERAVIS_DATA_DIR="$DATA" CERAVIS_MODELS_DIR="$APP/current/models" CERAVIS_CREDSTORE="$CRED" \
    bash -c '. "$1/setup/lib.sh" && write_layout' _ "$REL"

# Build everything against the NEW release folder; the services are pointed at
# /opt/ceravis/current, which is switched to it below.
export CERAVIS_ROOT="$REL" CERAVIS_USER="$SVC" CERAVIS_PYTHON="$REL/venv/bin/python" \
       CERAVIS_DATA_DIR="$DATA" CERAVIS_MODELS_DIR="$REL/models" CERAVIS_CREDSTORE="$CRED"
# shellcheck source=setup/lib.sh
. "$REL/setup/lib.sh"
export_layout

# ---- [5/9] software -------------------------------------------------------------
stage "5/9" "software (virtualenv)"
if [ -d "$SRC/wheels" ]; then export PIP_FIND_LINKS="$SRC/wheels"; fi
CERAVIS_VENV="$REL/venv" CERAVIS_WHEELHOUSE="$APP/wheels" bash "$REL/setup/install_native.sh"
"$REL/venv/bin/python" -m compileall -q "$REL/edge" >/dev/null 2>&1 || true

# ---- [6/9] AI models ---------------------------------------------------------------
stage "6/9" "AI models (TensorRT engines for this GPU)"
bash "$REL/setup/install_mediamtx.sh"
# The release carries every ONNX (made on the PC by setup/make_release.sh); only
# the engines are built here. A release without them exports them on this
# device first (needs internet and a throwaway torch venv).
if "$REL/venv/bin/python" "$REL/setup/export_models.py" --onnx-only >/dev/null 2>&1; then
    echo "  every ONNX model came with the release"
else
    echo "  ONNX models missing from the release — exporting on this device (needs internet)"
    bash "$REL/setup/export_engines.sh" --onnx-only
fi
"$REL/venv/bin/python" "$REL/setup/export_models.py"
rm -rf /tmp/ceravis-export-venv
lock_release "$REL"
echo "  $REL: root's, readable by $SVC only"

# Switch to this release now that it is complete (code, software, engines).
ln -sfn "$REL" "$APP/current.new" && mv -T "$APP/current.new" "$APP/current"
echo "  $APP/current -> $REL"
# From here on, units are written for the switching path, not the release path:
# the layout file's values (lib.sh reads it once nothing overrides it).
unset "${LAYOUT_KEYS[@]}"
. "$APP/current/setup/lib.sh"
export_layout

# ---- [7/9] secrets + tunnel -----------------------------------------------------------
stage "7/9" "secrets, SSH, hotspot rights and tunnel"
apply_secrets
setup_ssh
bash "$CERAVIS_ROOT/setup/install_hotspot.sh"

# ---- [8/9] identity ----------------------------------------------------------------------
stage "8/9" "this device's identity (first boot)"
install_unit "$EDGE_DIR/infra/systemd/ceravis-firstboot.service"
systemctl daemon-reload
systemctl enable ceravis-firstboot >/dev/null
rm -f /etc/ceravis/.firstboot-done
bash "$APP/current/setup/firstboot.sh"

# ---- [9/9] services -----------------------------------------------------------------------
stage "9/9" "services"
bash "$CERAVIS_ROOT/setup/install_service.sh" >/dev/null
bash "$CERAVIS_ROOT/setup/install_reboot_timer.sh"
bash "$CERAVIS_ROOT/setup/install_fleet_agent.sh" </dev/null
systemctl restart ceravis
bash "$CERAVIS_ROOT/setup/check_jetson.sh" || echo "  (the doctor flagged items above — the device still runs)"

NAME="$(device_name)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "============================================================"
echo "  CERAVIS $VERSION is installed on $NAME.   $(date -Is)"
echo
echo "  Open in a browser on the same network:  https://${IP:-<device-ip>}:8443"
echo "          or by name:                      https://$NAME.local:8443"
echo "  Through its own hotspot:                 https://10.42.0.1:8443"
echo "  (the browser asks once to trust this device's own certificate)"
echo
if ! curl -fsI --max-time 8 https://pypi.org >/dev/null 2>&1; then
    echo "  NO INTERNET: connect an Ethernet cable from the home router to the"
    echo "  device — its Wi-Fi runs the camera hotspot. The router gives it an"
    echo "  address by itself; nothing to set up."
    echo
fi
echo "  The first screen shows every readiness check and the hotspot's"
echo "  password; when it says READY, sign in with the care account's email."
[ "$HEADLESS" = 1 ] && echo "  Reboot once so the desktop is switched off:  sudo reboot"
echo "  Log of this install: $LOG"
echo "============================================================"
