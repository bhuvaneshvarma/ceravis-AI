#!/usr/bin/env bash
# Write what runs on this device, and its version, to ONE file:
#
#   /etc/ceravis/versions.json      (world-readable: it holds no secret)
#
#   release        the edge release (edge/VERSION), its commit and branch
#   fleet_agent    the fleet agent (edge/fleet/fms_agent) and its wire protocol
#   mediamtx       the media server binary          frpc   the tunnel client
#   python, l4t    the interpreter the services use, NVIDIA's L4T (JetPack) base
#   recorded_at    when this was written
#
# Every installer runs it as its last step — setup/install.sh and setup/setup.sh
# on a first install, setup/update.sh after every switch of release (and a
# rollback), setup/upgrade.sh on a development clone — so the file always says
# what is installed NOW. Read what is actually installed, never a copy of the
# pins in the install scripts, so it cannot drift from them.
#
# Run:  sudo bash setup/record_versions.sh        (prints the file)
set -euo pipefail

# shellcheck source=setup/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

OUT="${CERAVIS_VERSIONS_FILE:-/etc/ceravis/versions.json}"

first_line() { "$@" 2>/dev/null | head -1 | tr -d '\r' || true; }
MTX_BIN="${CERAVIS_MEDIAMTX_BIN:-/usr/local/bin/mediamtx}"
FRPC_BIN="${CERAVIS_FRPC_BIN:-/usr/local/bin/frpc}"
MTX=""; FRPC=""
[ -x "$MTX_BIN" ] && MTX="$(first_line "$MTX_BIN" --version)"
[ -x "$FRPC_BIN" ] && FRPC="$(first_line "$FRPC_BIN" --version)"
GIT_COMMIT="$(git -C "$CERAVIS_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
GIT_BRANCH="$(git -C "$CERAVIS_ROOT" branch --show-current 2>/dev/null || true)"

JSON="$(MTX="$MTX" FRPC="$FRPC" GIT_COMMIT="$GIT_COMMIT" GIT_BRANCH="$GIT_BRANCH" \
        "$CERAVIS_PYTHON" -I - "$CERAVIS_ROOT" <<'PY'
import json, os, platform, re, sys, time
root = sys.argv[1]

def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""

def manifest():          # a release names its commit and branch here
    try:
        return json.loads(read(os.path.join(root, "manifest.json")) or "{}")
    except ValueError:
        return {}

def agent():             # the agent's own version and protocol, read as text
    ver = re.search(r'__version__\s*=\s*"([^"]+)"',
                    read(os.path.join(root, "edge/fleet/fms_agent/__init__.py")))
    proto = re.search(r"^VERSION\s*=\s*(\d+)",
                      read(os.path.join(root, "edge/fleet/fms_protocol/__init__.py")), re.M)
    return {"version": ver.group(1) if ver else "",
            "protocol": int(proto.group(1)) if proto else None}

m = manifest()
l4t = read("/etc/nv_tegra_release").splitlines()
print(json.dumps({
    "release": {"version": read(os.path.join(root, "edge/VERSION")).strip(),
                "commit": m.get("commit") or os.environ.get("GIT_COMMIT", ""),
                "branch": m.get("branch") or os.environ.get("GIT_BRANCH", "")},
    "fleet_agent": agent(),
    "mediamtx": os.environ.get("MTX", ""),
    "frpc": os.environ.get("FRPC", ""),
    "python": platform.python_version(),
    "l4t": l4t[0].lstrip("# ").strip() if l4t else "",
    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}, indent=2))
PY
)"

TMP="$(mktemp)"
printf '%s\n' "$JSON" > "$TMP"
sudo install -d -m 0755 "$(dirname "$OUT")"
sudo install -m 0644 "$TMP" "$OUT"
rm -f "$TMP"
cat "$OUT"
