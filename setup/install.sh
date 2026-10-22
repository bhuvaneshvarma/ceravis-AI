#!/usr/bin/env bash
# =============================================================
# CERAVIS — ONE-COMMAND PRODUCTION INSTALL (Jetson Orin Nano, JetPack 6)
# =============================================================
# Takes a device with JetPack installed (SDK Manager) to a running CERAVIS
# edge, ready for its first sign-in at  http://<device-ip>:8000
#
#   [1/9] check     the device, JetPack, disk space, internet
#   [2/9] system    no desktop, power mode, fan, NVIDIA packages held, logs
#   [3/9] account   the ceravis-svc service account and its folders
#   [4/9] release   this version into /opt/ceravis/releases/<version>
#   [5/9] software  Python packages in the release's own virtualenv
#   [6/9] AI models TensorRT engines built for this GPU
#   [7/9] secrets   app-server key, fleet key, tunnel — root-only files
#   [8/9] identity  device name, camera hotspot, keys (first boot)
#   [9/9] services  start everything, then the full health check
#
# Production layout (setup/lib.sh):
#   /opt/ceravis/releases/<v>/   code + virtualenv + engines (one per version)
#   /opt/ceravis/current         -> the release in use (switched by update.sh)
#   /var/lib/ceravis/            everything the device writes (data)
#   /etc/ceravis/credstore/      secrets, root-only, handed over as credentials
#
# Secrets come from a file you keep OFF the release (it is never in the repo):
#   CERAVIS_API_KEY=…   FMS_ENROLL_KEY=…   FRP_SERVER_ADDR=…   FRP_TOKEN=…
# looked for at --secrets FILE, then ceravis-secrets.env next to the release
# folder, then in it. Anything missing can be added later by re-running.
#
# Run:  sudo bash install.sh [--secrets FILE] [--power-mode 25W|MAXN_SUPER|15W]
#                            [--keep-desktop]
# Re-running is safe: finished work is kept, the same version is refreshed.
# =============================================================
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # the release folder
SECRETS=""; POWER="${POWER_MODE:-25W}"; HEADLESS=1
while [ $# -gt 0 ]; do
    case "$1" in
        --secrets) SECRETS="$2"; shift 2 ;;
        --power-mode) POWER="$2"; shift 2 ;;
        --keep-desktop) HEADLESS=0; shift ;;
        -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
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

stage() { echo; echo "============ [$1] $2 ============"; }
fail()  { echo "INSTALL STOPPED: $*" >&2; exit 1; }

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
    [ -d "$SRC/wheels" ] || fail "no internet and no wheels/ in the release — connect the device to the internet"
    echo "  internet: no — installing from the release's own wheels/"
fi
echo "  CERAVIS $VERSION from $SRC, JetPack $(head -1 /etc/nv_tegra_release | grep -oE 'R36[^,]*, REVISION: [0-9.]+')"

# Secrets: KEY=value lines; only these keys are taken, nothing is executed.
if [ -z "$SECRETS" ]; then
    for f in "$(dirname "$SRC")/ceravis-secrets.env" "$SRC/ceravis-secrets.env"; do
        [ -f "$f" ] && { SECRETS="$f"; break; }
    done
fi
if [ -n "$SECRETS" ]; then
    [ -f "$SECRETS" ] || fail "secrets file not found: $SECRETS"
    while IFS='=' read -r k v; do
        case "$k" in
            CERAVIS_API_KEY|FMS_ENROLL_KEY|FRP_SERVER_ADDR|FRP_TOKEN)
                v="${v%$'\r'}"; v="${v%\"}"; v="${v#\"}"; export "$k=$v" ;;
        esac
    done < "$SECRETS"
    echo "  secrets: $SECRETS"
else
    echo "  secrets: none given (add later: sudo bash $APP/current/setup/install.sh --secrets FILE)"
fi

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
install -d -m 0700 -o root -g root /etc/ceravis "$CRED"
install -d -m 0755 "$APP" "$APP/releases"

# ---- [4/9] release ------------------------------------------------------------
stage "4/9" "release $VERSION"
if [ "$SRC" != "$REL" ]; then
    mkdir -p "$REL"
    # Code only: a re-run keeps the release's virtualenv and built engines.
    tar -C "$SRC" --exclude=./.git --exclude=./dist --exclude=./venv --exclude=./models --exclude=./wheels -cf - . | tar -C "$REL" -xf -
fi
install -d -m 0755 -o "$SVC" -g "$SVC" "$REL/models"
# Engines from a release built before on this device are copied, not rebuilt,
# when they come from the same ONNX (update.sh does the same).
if [ -d "$SRC/models-src" ]; then
    (cd "$SRC/models-src" && find . -type f -name '*.onnx') | while IFS= read -r f; do
        install -D -m 0644 -o "$SVC" -g "$SVC" "$SRC/models-src/$f" "$REL/models/$f"
    done
    echo "  ONNX models from the release copied in"
fi
chown -R root:root "$REL/edge" "$REL/setup" "$REL/cloud"

cat > /etc/ceravis/layout.env <<EOF
# Written by setup/install.sh — the production layout (setup/lib.sh).
CERAVIS_ROOT=$APP/current
CERAVIS_USER=$SVC
CERAVIS_PYTHON=$APP/current/venv/bin/python
CERAVIS_DATA_DIR=$DATA
CERAVIS_MODELS_DIR=$APP/current/models
CERAVIS_CREDSTORE=$CRED
EOF
chmod 0644 /etc/ceravis/layout.env

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
need_onnx() { [ ! -s "$REL/models/$1" ]; }
if need_onnx detection/yolo26m.onnx || need_onnx pose/yolo26m-pose.onnx; then
    echo "  detection/pose ONNX not in the release — exporting on this device (needs internet)"
    bash "$REL/setup/export_engines.sh"
fi
need_onnx reid/reid.onnx && bash "$REL/setup/export_reid.sh"
"$REL/venv/bin/python" "$REL/setup/export_models.py"
chown -R "$SVC:$SVC" "$REL/models"
rm -rf /tmp/ceravis-export-venv

# ---- [7/9] secrets + tunnel -----------------------------------------------------------
stage "7/9" "secrets, hotspot rights and tunnel"
bash "$REL/setup/install_api_key.sh"
bash "$REL/setup/install_hotspot.sh"
bash "$REL/cloud/install_frpc.sh"

# Switch to this release now that it is complete.
ln -sfn "$REL" "$APP/current.new" && mv -T "$APP/current.new" "$APP/current"
echo "  $APP/current -> $REL"
# From here on, units are written for the switching path, not the release path.
# shellcheck disable=SC1091
set -a; . /etc/ceravis/layout.env; set +a
. "$APP/current/setup/lib.sh"
export_layout

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
echo "  CERAVIS $VERSION is installed on $NAME."
echo
echo "  Open in a browser on the same network:  http://${IP:-<device-ip>}:8000"
echo "          or by name:                      http://$NAME.local:8000"
echo "  Through its own hotspot:                 http://10.42.0.1:8000"
echo
echo "  The first screen shows every readiness check and the hotspot's"
echo "  password; when it says READY, sign in with the care account's email."
[ "$HEADLESS" = 1 ] && echo "  Reboot once so the desktop is switched off:  sudo reboot"
echo "============================================================"
