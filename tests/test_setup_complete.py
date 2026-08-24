"""
One command must actually bring up the WHOLE device.

setup/setup.sh silently skipped the ReID engine export, the frp tunnel and the
reboot timer. A device set up that way looks fine — it boots, it records, the
monitor loads — but it never IDENTIFIES anyone (no ReID engine means the whole
AI layer stays gated, announced only as one INFO line at startup) and it never
learns its edge_id into the tunnel, so every live link stays dead.

This is a STATIC check on the scripts. It cannot start a Jetson, so it does the
next best thing: it asserts the one-command path REFERENCES every stage a bare
device needs, and that ordering constraints hold.

Run:  python tests/test_setup_complete.py
"""
import io
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup" / "setup.sh"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


src = io.open(SETUP, encoding="utf-8").read()


print("\n1. every bring-up stage is invoked")
REQUIRED = [
    ("runtime deps",        "install_native.sh"),
    ("media backbone",      "install_mediamtx.sh"),
    ("detect + pose TRT",   "export_engines.sh"),
    ("ReID TRT engine",     "export_reid.sh"),
    ("fleet tunnel + helper", "install_frpc.sh"),
    ("app service",         "install_service.sh"),
    ("nightly reboot timer", "install_reboot_timer.sh"),
    ("verification gate",   "check_jetson.sh"),
]
for label, script in REQUIRED:
    check(f"{label} ({script})", script in src)


print("\n2. the scripts it calls actually exist")
for _label, script in REQUIRED:
    hits = list(ROOT.rglob(script))
    check(f"{script} present in the repo", bool(hits),
          hits[0].relative_to(ROOT).as_posix() if hits else "MISSING")


print("\n3. the env file is generated BEFORE anything reads it")
env_at = src.find("jetson.env.example")
check("setup generates jetson.env from the template", env_at != -1)
if env_at != -1:
    for label, script in REQUIRED:
        pos = src.find(script)
        check(f"config precedes {script}", pos > env_at,
              f"env@{env_at} vs {script}@{pos}")
check("it never clobbers an existing jetson.env",
      re.search(r'if \[ -f "\$ENV_DIR/jetson\.env" \]', src) is not None)


print("\n4. the template is what ships, not a live config")
example = ROOT / "edge/infra/env/jetson.env.example"
check("jetson.env.example exists", example.exists())
if example.exists():
    body = io.open(example, encoding="utf-8").read()
    live = [l for l in body.splitlines()
            if re.match(r"^(EDGE_ID|CERAVIS_API_KEY)=\S", l)]
    check("template carries no per-device secrets", not live, str(live[:3]))


print("\n5. the tunnel installer wires the privileged edge_id helper")
# Without this, account verification writes EDGE_ID to jetson.env but can never
# push it into frpc.toml — the device is provisioned and still unreachable.
frpc = ROOT / "cloud" / "install_frpc.sh"
check("cloud/install_frpc.sh exists", frpc.exists())
if frpc.exists():
    f = io.open(frpc, encoding="utf-8").read()
    check("installs ceravis-apply-edge-id", "ceravis-apply-edge-id" in f)
    check("grants it a NOPASSWD sudoers rule", "NOPASSWD" in f and "sudoers.d" in f)


print("\n6. setup is re-runnable (idempotence is claimed AND implemented)")
check("documents re-running as the recovery path", "RE-RUN THIS SAME SCRIPT" in src)
check("aborts on first error", "set -euo pipefail" in src)
check("reboot password never blocks a non-interactive run",
      "-t 0" in src, "must test for a terminal before prompting")


print("\n7. service topology: grouped for control, NOT coupled in lifecycle")
TARGET = ROOT / "edge/infra/systemd/ceravis.target"
check("ceravis.target exists", TARGET.exists())
if TARGET.exists():
    t = io.open(TARGET, encoding="utf-8").read()
    for unit in ("ceravis.service", "frpc.service", "ceravis-reboot.timer"):
        check(f"target pulls in {unit}", f"Wants={unit}" in t)
    # BindsTo/Requires/PartOf would make every edge_id change restart the AI
    # pipeline and every code deploy drop the tunnel — the tunnel being the
    # remote route used to diagnose the deploy.
    # Only DIRECTIVE lines count. The target explains at length WHY it avoids
    # these, and a check that trips over its own rationale is a broken check.
    directives = [l.strip() for l in t.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    for banned in ("BindsTo=", "Requires=", "PartOf="):
        check(f"no {banned} coupling",
              not any(d.startswith(banned) for d in directives))
    check("the reasoning is written down, not just applied",
          "BindsTo" in t and "failure domain" in t)

INSTALLER = io.open(ROOT / "setup/install_service.sh", encoding="utf-8").read()
check("the installer installs the target", "ceravis.target" in INSTALLER)
check("and enables it", "enable ceravis.target" in INSTALLER)


print("\n8. edge_id application is VERIFIED, not just attempted")
prov = io.open(ROOT / "edge/integration/edge_provision.py", encoding="utf-8").read()
check("tunnel_status() reads frpc.toml back", "def tunnel_status" in prov
      and "frpc.toml" in prov)
check("the apply path retries and verifies",
      "def apply_edge_id_verified" in prov)
main_py = io.open(ROOT / "edge/main.py", encoding="utf-8").read()
check("boot self-heal uses the verified path",
      "apply_edge_id_verified" in main_py)
acct = io.open(ROOT / "edge/api/account_routes.py", encoding="utf-8").read()
check("verification writes EDGE_ID to the env file", "set_env_value" in acct)
check("and schedules the tunnel apply", "apply_edge_id_async" in acct)
sysr = io.open(ROOT / "edge/api/system_routes.py", encoding="utf-8").read()
check("status surfaces whether the tunnel is really keyed",
      '"tunnel"' in sysr and "_tunnel_status" in sysr)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll setup-completeness checks passed.")
