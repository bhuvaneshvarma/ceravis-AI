"""
The installer's building blocks, run for real in bash (sudo stubbed, nothing
on the system touched):

1. every setup script parses (bash -n)
2. install_unit writes the development unit as before, and the production one
   with the service account, the virtualenv, DATA_DIR/MODELS_DIR and the API
   key as a systemd credential (no secrets EnvironmentFile)
3. frpc starts only on a real config — the ExecCondition taken from the unit
   file itself: template, server-but-no-account -> off; claimed -> on
4. frpc_set_user adds then replaces the device prefix, and refuses a bad name;
   the edge_id helper keeps it; frpc_reset_routes (seal) puts every route and
   the prefix back to placeholders but keeps the shared fleet domain
5. a release bundle holds the committed code, the installer and a manifest
   whose hashes match — and none of the dev/test/cloud-server files

Needs bash and git (Git Bash on Windows). Run:  python tests/test_installer.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def sh(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    stub = 'sudo() { if [ "$1" = tee ]; then cat > "$OUT/$(basename "$2")"; ' \
           'elif [ "$1" = test ]; then shift; test "$@"; else "$@"; fi; }\n'
    return subprocess.run([BASH, "-c", stub + script], cwd=ROOT, capture_output=True,
                          text=True, env={**os.environ, **(env or {})})


def posix(p: Path) -> str:
    """A path bash on this OS understands (Git Bash: /c/Users/...)."""
    s = str(p).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)", s)
    return f"/{m.group(1).lower()}/{m.group(2)}" if m and os.name == "nt" else s


if not BASH:
    print("bash not available — skipped")
    sys.exit(0)

print("\n1. every setup script parses")
scripts = sorted([*ROOT.glob("setup/*.sh"), *ROOT.glob("setup/usb/*.sh"), *ROOT.glob("cloud/*.sh")])
for s in scripts:
    r = subprocess.run([BASH, "-n", posix(s)], capture_output=True, text=True)
    check(s.relative_to(ROOT).as_posix(), r.returncode == 0, r.stderr.strip()[:120])

print("\n2. unit files for both layouts")
out = Path(tempfile.mkdtemp(prefix="ceravis-units-"))
cred = out / "cred"
cred.mkdir()
(cred / "ceravis_api_key").write_text("k")
r = sh("source setup/lib.sh; install_unit edge/infra/systemd/ceravis.service",
       {"OUT": posix(out), "USER": "ceravis", "SUDO_USER": ""})
dev = (out / "ceravis.service").read_text() if (out / "ceravis.service").exists() else ""
check("development unit written", "User=ceravis" in dev and "secrets.env" in dev, r.stderr[-200:])
(out / "ceravis.service").unlink(missing_ok=True)
prod_env = {"OUT": posix(out), "CERAVIS_ROOT": "/opt/ceravis/current",
            "CERAVIS_USER": "ceravis-svc",
            "CERAVIS_PYTHON": "/opt/ceravis/current/venv/bin/python",
            "CERAVIS_DATA_DIR": "/var/lib/ceravis",
            "CERAVIS_MODELS_DIR": "/opt/ceravis/current/models",
            "CERAVIS_CREDSTORE": posix(cred)}
sh("source setup/lib.sh; install_unit edge/infra/systemd/ceravis.service", prod_env)
prod = (out / "ceravis.service").read_text() if (out / "ceravis.service").exists() else ""
check("production: service account", "User=ceravis-svc" in prod)
check("production: the release's virtualenv runs the app",
      "ExecStart=/opt/ceravis/current/venv/bin/python -m uvicorn" in prod)
check("production: data and models folders set",
      "Environment=DATA_DIR=/var/lib/ceravis" in prod
      and "Environment=MODELS_DIR=/opt/ceravis/current/models" in prod)
check("production: API key as a credential, no secrets env file",
      "LoadCredential=ceravis_api_key:" in prod and "secrets.env" not in prod)
check("production: no development path left", "/home/ceravis/ceravis2" not in prod)

print("\n3. the tunnel starts only on a real config")
unit = (ROOT / "cloud/systemd/frpc.service").read_text()
cond = re.search(r'^ExecCondition=/bin/sh -c "(.*)"$', unit, re.M)
check("frpc.service has an ExecCondition", cond is not None)
if cond:
    conf = out / "frpc.toml"
    probe = cond.group(1).replace("/etc/frp/frpc.toml", posix(conf))

    def starts(text: str) -> bool:
        conf.write_text(text)
        return sh(probe).returncode == 0

    template = (ROOT / "cloud/frpc.toml.example").read_text()
    server = (template.replace("EC2_PUBLIC_IP_OR_DOMAIN", "1.2.3.4")
              .replace("CHANGE_ME_TO_A_LONG_RANDOM_SECRET", "tok"))
    check("the template -> stays off", not starts(template))
    check("server set, no account yet -> stays off", not starts(server))
    check("claimed (routes keyed) -> starts", starts(server.replace("/EDGE_ID", "/Abc123")))

print("\n4. device prefix, edge_id helper, seal reset")
conf = out / "frpc2.toml"
conf.write_text((ROOT / "cloud/frpc.toml.example").read_text())
c = posix(conf)
sh(f'source setup/lib.sh; frpc_set_user "{c}" ceravis-acg08mbv3', {"OUT": posix(out)})
txt = conf.read_text()
check("prefix set", 'user = "ceravis-acg08mbv3"' in txt)
sh(f'source setup/lib.sh; frpc_set_user "{c}" ceravis-zz9', {"OUT": posix(out)})
txt = conf.read_text()
check("prefix replaced, never duplicated",
      'user = "ceravis-zz9"' in txt and txt.count("# ceravis:device-name") == 1)
bad = sh(f'source setup/lib.sh; frpc_set_user "{c}" "x; rm -rf /"', {"OUT": posix(out)})
check("a bad name is refused", bad.returncode != 0 and 'user = "ceravis-zz9"' in conf.read_text())
bindir = out / "bin"
bindir.mkdir()
(bindir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
os.chmod(bindir / "systemctl", 0o755)
r = subprocess.run([BASH, posix(ROOT / "cloud/apply_edge_id.sh"), "Vzsp-Nrq123"], cwd=ROOT,
                   capture_output=True, text=True,
                   env={**os.environ, "FRPC_TOML": c,
                        "PATH": posix(bindir) + os.pathsep + os.environ.get("PATH", "")})
txt = conf.read_text()
check("the edge_id helper keys the routes and keeps the prefix",
      'locations = ["/Vzsp-Nrq123"]' in txt and 'user = "ceravis-zz9"' in txt, r.stderr[-150:])
sh(f'source setup/lib.sh; frpc_reset_routes "{c}"', {"OUT": posix(out)})
txt = conf.read_text()
check("seal: routes back to /EDGE_ID, prefix back to DEVICE_NAME",
      "Vzsp" not in txt and 'locations = ["/EDGE_ID"]' in txt
      and 'user = "DEVICE_NAME"' in txt)
check("seal: the shared fleet domain is kept",
      'customDomains = ["edgeai.ceravishealth.in"]' in txt)

print("\n5. a release bundle")
if shutil.which("git"):
    dist = ROOT / "dist"
    keep = dist.exists()
    r = subprocess.run([BASH, posix(ROOT / "setup/make_release.sh"), "--allow-dirty"],
                       cwd=ROOT, capture_output=True, text=True)
    version = (ROOT / "edge/VERSION").read_text().strip()
    bundle = dist / f"ceravis-edge-{version}.tar.gz"
    check("built", r.returncode == 0 and bundle.exists(), (r.stderr or r.stdout)[-200:])
    if bundle.exists():
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        sha_file = (dist / f"{bundle.name}.sha256").read_text().split()[0]
        check("its .sha256 matches", digest == sha_file)
        with tarfile.open(bundle) as tf:
            names = tf.getnames()
            top = f"ceravis-edge-{version}/"
            manifest = json.load(tf.extractfile(top + "manifest.json"))
            check("manifest names version and commit",
                  manifest["version"] == version and manifest["commit"])
            sample = top + "edge/main.py"
            check("manifest hashes match the files",
                  manifest["files"]["edge/main.py"]
                  == hashlib.sha256(tf.extractfile(sample).read()).hexdigest())
        rel = {n[len(top):] for n in names if n.startswith(top)}
        check("the one command is there", "install.sh" in rel and "setup/install.sh" in rel)
        for gone in ("tests/", "edge/static/ui-testing/", "cloud/frps.toml", ".claude/",
                     "setup/upgrade.sh", "setup/make_release.sh", "setup/usb/"):
            check(f"left out: {gone}", not any(p == gone.rstrip("/") or p.startswith(gone)
                                              for p in rel))
        check("no secrets file inside", not any("secrets.env" in p and "example" not in p
                                                for p in rel))
        bundle.unlink()
        (dist / f"{bundle.name}.sha256").unlink(missing_ok=True)
        if not keep:
            shutil.rmtree(dist, ignore_errors=True)
else:
    print("  (git not available — bundle checks skipped)")

shutil.rmtree(out, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All installer checks passed.")
