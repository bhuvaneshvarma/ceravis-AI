# shellcheck shell=bash
# =============================================================
# Shared by every setup script: ONE definition of where CERAVIS lives and who
# runs it, and ONE way to install a systemd unit.
#
# Two layouts, chosen by the caller's environment (defaults = development):
#
#   development (setup/setup.sh, the git clone):
#       code + data in the clone (edge/data, edge/models), services run as the
#       user who installs, system python, secrets in /etc/ceravis/secrets.env
#
#   production (setup/install.sh):
#       CERAVIS_ROOT=/opt/ceravis/current        the release in use (read-only)
#       CERAVIS_USER=ceravis-svc                 a service account, no login
#       CERAVIS_PYTHON=<release>/venv/bin/python its own virtualenv
#       CERAVIS_DATA_DIR=/var/lib/ceravis        everything the device writes
#       CERAVIS_MODELS_DIR=/var/lib/ceravis/models  engines (survive updates)
#       CERAVIS_CREDSTORE=/etc/ceravis/credstore root-only secret files,
#                                                handed over as systemd credentials
# =============================================================

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERAVIS_ROOT="${CERAVIS_ROOT:-$(dirname "$LIB_DIR")}"
CERAVIS_USER="${CERAVIS_USER:-${SUDO_USER:-$USER}}"
CERAVIS_PYTHON="${CERAVIS_PYTHON:-/usr/bin/python3}"
CERAVIS_DATA_DIR="${CERAVIS_DATA_DIR:-}"
CERAVIS_MODELS_DIR="${CERAVIS_MODELS_DIR:-}"
CERAVIS_CREDSTORE="${CERAVIS_CREDSTORE:-}"
EDGE_DIR="$CERAVIS_ROOT/edge"

# The secret files the app reads as systemd credentials (pydantic secrets_dir:
# the file name is the setting name).
CREDENTIALS=(ceravis_api_key)

data_dir()   { if [ -n "$CERAVIS_DATA_DIR" ];   then echo "$CERAVIS_DATA_DIR";   else echo "$EDGE_DIR/data";   fi; }
models_dir() { if [ -n "$CERAVIS_MODELS_DIR" ]; then echo "$CERAVIS_MODELS_DIR"; else echo "$EDGE_DIR/models"; fi; }

# Export the layout so child scripts and the app's own settings agree on it.
export_layout() {
    export CERAVIS_ROOT CERAVIS_USER CERAVIS_PYTHON CERAVIS_DATA_DIR CERAVIS_MODELS_DIR CERAVIS_CREDSTORE
    [ -n "$CERAVIS_DATA_DIR" ] && export DATA_DIR="$CERAVIS_DATA_DIR"
    [ -n "$CERAVIS_MODELS_DIR" ] && export MODELS_DIR="$CERAVIS_MODELS_DIR"
    return 0
}

# Install a unit template for this layout. Templates are written for the
# development clone (/home/ceravis/ceravis2, User=ceravis, /usr/bin/python3);
# this swaps in the real root, account and interpreter, and adds the production
# environment and credentials right after [Service].
#   install_unit <template> [unit name]
install_unit() {
    local src="$1" name="${2:-$(basename "$1")}" extra=""
    [ -n "$CERAVIS_DATA_DIR" ]   && extra+="Environment=DATA_DIR=$CERAVIS_DATA_DIR\n"
    [ -n "$CERAVIS_MODELS_DIR" ] && extra+="Environment=MODELS_DIR=$CERAVIS_MODELS_DIR\n"
    if [ -n "$CERAVIS_CREDSTORE" ] && [ "$name" = "ceravis.service" ]; then
        local c
        for c in "${CREDENTIALS[@]}"; do
            sudo test -s "$CERAVIS_CREDSTORE/$c" \
                && extra+="LoadCredential=$c:$CERAVIS_CREDSTORE/$c\n"
        done
    fi
    sed -e "s|/home/ceravis/ceravis2|$CERAVIS_ROOT|g" \
        -e "s|^User=.*|User=$CERAVIS_USER|" \
        -e "s|/usr/bin/python3|$CERAVIS_PYTHON|g" \
        "$src" \
    | { if [ -n "$CERAVIS_CREDSTORE" ]; then grep -v '^EnvironmentFile=-/etc/ceravis/secrets.env'; else cat; fi; } \
    | awk -v extra="$extra" '{ print } /^\[Service\]/ && extra != "" { printf "%s", extra }' \
    | sudo tee "/etc/systemd/system/$name" >/dev/null
}

# Install a sudoers drop-in only if visudo accepts it — a malformed file locks
# everyone out of sudo.   install_sudoers <name> <rule line>
install_sudoers() {
    local tmp
    tmp="$(mktemp)"
    printf '%s\n' "$2" > "$tmp"
    if sudo visudo -cf "$tmp" >/dev/null; then
        sudo install -m 0440 -o root -g root "$tmp" "/etc/sudoers.d/$1"
    else
        echo "REFUSED: sudoers rule for $1 failed validation — not installed" >&2
        rm -f "$tmp"; return 1
    fi
    rm -f "$tmp"
}

# frp proxy names must be unique on the server: prefix them with this device's
# own name (frpc's `user`).   frpc_set_user <frpc.toml> <name>
frpc_set_user() {
    local file="$1" name="$2"
    [[ "$name" =~ ^[a-z0-9-]{3,63}$ ]] || { echo "refusing frp user '$name'" >&2; return 2; }
    if sudo grep -q '# ceravis:device-name' "$file"; then
        sudo sed -i "s|^user[[:space:]]*=.*# ceravis:device-name|user = \"$name\"   # ceravis:device-name|" "$file"
    else
        sudo sed -i "0,/^serverAddr/s||user = \"$name\"   # ceravis:device-name\n&|" "$file"
    fi
}

# Put a tunnel config back to its placeholders (seal.sh): no edge_id routes, no
# device prefix — the server address and token are kept.
#   frpc_reset_routes <frpc.toml>
frpc_reset_routes() {
    sudo sed -i \
        -e 's|^locations = \["/[^"/]*"\].*# ceravis:edge-id|locations = ["/EDGE_ID"]  # ceravis:edge-id|' \
        -e 's|^locations = \["/[^"/]*/api", "/[^"/]*/ui"\].*# ceravis:edge-id|locations = ["/EDGE_ID/api", "/EDGE_ID/ui"]  # ceravis:edge-id|' \
        -e 's|^customDomains = \["[^"]*"\].*# ceravis:ssh-edge-id|customDomains = ["EDGE_ID"]  # ceravis:ssh-edge-id|' \
        -e 's|^user[[:space:]]*=.*# ceravis:device-name|user = "DEVICE_NAME"   # ceravis:device-name|' \
        "$1"
}

# This device's name (ceravis-<code>) from the app's own identity module.
device_name() { (cd "$EDGE_DIR" && "$CERAVIS_PYTHON" -m common.device_identity name); }
