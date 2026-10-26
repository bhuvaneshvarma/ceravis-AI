"""
The installer's building blocks, run for real in bash (sudo stubbed, nothing
on the system touched):

1. every setup script parses (bash -n)
2. install_unit writes the development unit and the production one (service
   account, virtualenv, DATA_DIR/MODELS_DIR); both get the API key as a systemd
   credential, only once it exists, and no secrets EnvironmentFile
2b. the ONE layout file (/etc/ceravis/layout.env): write_layout writes it,
   every script reads it back as data, the environment wins over it
3. frpc starts only on a real config — the ExecCondition taken from the unit
   file itself: template, server-but-no-account -> off; claimed -> on
4. frpc_set_user adds then replaces the device prefix, and refuses a bad name;
   the edge_id helper keeps it; frpc_reset_routes (seal) puts every route and
   the prefix back to placeholders but keeps the shared fleet domain
5. a release is ONE folder (installer, bundle, fingerprint, README — never a
   secret); the bundle holds the committed code, the installer and a manifest
   whose hashes match — and none of the dev/test/cloud-server files
6. install.sh reads the secrets file as data: spaces, quotes, CRLF, a last line
   without a newline; nothing executed, unknown keys ignored
7. install_frpc.sh keeps a keyed tunnel config when server and token are the same

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
           'elif [ "$1" = test ]; then shift; test "$@"; ' \
           'elif [ "$1" = install ]; then shift; ' \
           'install $(printf "%s\\n" "$@" | grep -vE "^-[og]$|^root$"); else "$@"; fi; }\n'
    base = {k: v for k, v in os.environ.items() if not k.startswith("CERAVIS_")}
    return subprocess.run([BASH, "-c", stub + script], cwd=ROOT, capture_output=True,
                          text=True, env={**base, "CERAVIS_LAYOUT_FILE": "/nonexistent",
                                          **(env or {})})


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
       {"OUT": posix(out), "USER": "ceravis", "SUDO_USER": "", "CERAVIS_CREDSTORE": posix(cred)})
dev = (out / "ceravis.service").read_text() if (out / "ceravis.service").exists() else ""
check("development unit written", "User=ceravis" in dev
      and "ExecStart=/usr/bin/python3 -m serve" in dev, r.stderr[-200:])
check("development: the API key as a credential too, no secrets env file",
      "LoadCredential=ceravis_api_key:" in dev and "secrets.env" not in dev)
check("development: data and engines stay in the clone",
      "Environment=DATA_DIR" not in dev and "Environment=MODELS_DIR" not in dev)
empty = out / "empty-cred"
empty.mkdir()
sh("source setup/lib.sh; install_unit edge/infra/systemd/ceravis.service",
   {"OUT": posix(out), "USER": "ceravis", "SUDO_USER": "", "CERAVIS_CREDSTORE": posix(empty)})
check("no key yet -> no credential line (systemd would refuse a missing file)",
      not re.search(r"^LoadCredential=", (out / "ceravis.service").read_text(), re.M))
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
      "ExecStart=/opt/ceravis/current/venv/bin/python -m serve" in prod)
check("production: data and models folders set",
      "Environment=DATA_DIR=/var/lib/ceravis" in prod
      and "Environment=MODELS_DIR=/opt/ceravis/current/models" in prod)
check("production: API key as a credential, no secrets env file",
      "LoadCredential=ceravis_api_key:" in prod and "secrets.env" not in prod)
check("production: no development path left", "/home/ceravis/ceravis2" not in prod)

print("\n2b. one layout file for every script")
layout = out / "layout.env"
probe = 'source setup/lib.sh; echo "$CERAVIS_ROOT|$CERAVIS_USER|$CERAVIS_PYTHON|$(data_dir)|$(models_dir)|$CERAVIS_CREDSTORE"'
lay_env = {"OUT": posix(out), "CERAVIS_LAYOUT_FILE": posix(layout)}
r = sh("source setup/lib.sh; write_layout", {**lay_env, **{k: v for k, v in prod_env.items()
                                                          if k != "OUT"}})
check("write_layout writes the file", layout.exists(), r.stderr[-200:])
want = "|".join(prod_env[k] for k in ("CERAVIS_ROOT", "CERAVIS_USER", "CERAVIS_PYTHON",
                                      "CERAVIS_DATA_DIR", "CERAVIS_MODELS_DIR",
                                      "CERAVIS_CREDSTORE"))
got = sh(probe, lay_env).stdout.strip()
check("a script run later reads the production layout from it", got == want, got)
got = sh(probe, {**lay_env, "CERAVIS_ROOT": "/opt/ceravis/releases/9.9.9"}).stdout.strip()
check("a value in the environment wins (an installer staging a new release)",
      got.startswith("/opt/ceravis/releases/9.9.9|ceravis-svc|"), got)
layout.write_bytes(b"CERAVIS_USER=ceravis-svc\r\nCERAVIS_DATA_DIR=/var/lib/ceravis")
got = sh(probe, lay_env).stdout.strip().split("|")
check("CRLF and a last line without a newline are read",
      got[1] == "ceravis-svc" and got[3] == "/var/lib/ceravis", "|".join(got))
layout.write_text("CERAVIS_USER=x; rm -rf /tmp/never\nPATH=/evil\n")
got = sh('source setup/lib.sh; echo "$CERAVIS_USER|$PATH"', lay_env).stdout.strip()
check("read as data: nothing executed, other keys ignored",
      got.startswith("x; rm -rf /tmp/never|") and "/evil" not in got, got)
got = sh(probe, {"OUT": posix(out)}).stdout.strip().split("|")
norm = lambda p: p.replace("\\", "/").rstrip("/").lower()    # noqa: E731
check("no layout file -> the development clone",
      norm(got[0]) in (norm(str(ROOT)), norm(posix(ROOT))) and got[3] == got[0] + "/edge/data"
      and got[4] == got[0] + "/edge/models" and got[5] == "/etc/ceravis/credstore",
      "|".join(got))

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

print("\n5. a release folder")
if shutil.which("git"):
    dist, pc = out / "dist", out / "pc"
    pc.mkdir()
    (pc / "ceravis-secrets.env").write_text("CERAVIS_API_KEY=never-in-the-folder\n")
    # The device's package list, as `apt-get install --print-uris` writes it.
    repo = out / "aptrepo"
    repo.mkdir()
    pkg = repo / "gstreamer1.0-plugins-bad_1.0_arm64.deb"
    pkg.write_bytes(b"a system package")
    md5 = hashlib.md5(pkg.read_bytes()).hexdigest()
    debs = pc / "debs.txt"
    debs.write_text(f"'{pkg.resolve().as_uri()}' {pkg.name} {pkg.stat().st_size} MD5Sum:{md5}\r\n")

    def release(*extra):
        return subprocess.run([BASH, posix(ROOT / "setup/make_release.sh"), "--allow-dirty",
                               "--no-models", "--no-vendor", "--no-wheels", *extra], cwd=ROOT,
                              capture_output=True, text=True,
                              env={**os.environ, "CERAVIS_DIST": posix(dist),
                                   "CERAVIS_PC_DIR": posix(pc)})

    r = release()
    version = (ROOT / "edge/VERSION").read_text().strip()
    folder = dist / "CERAVIS"
    bundle = folder / f"ceravis-edge-{version}.tar.gz"
    check("built into ONE folder", r.returncode == 0 and bundle.exists(),
          (r.stderr or r.stdout)[-200:])
    check("the folder: its installer, the release, its fingerprint, the README — nothing else",
          sorted(p.name for p in folder.iterdir()) == sorted(
              ["install.sh", "README.txt", bundle.name, bundle.name + ".sha256"]),
          str(sorted(p.name for p in folder.iterdir())) if folder.exists() else "")
    check("no second copy of the release anywhere in dist/",
          [p for p in dist.rglob("*.tar.gz")] == [bundle])
    if bundle.exists():
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        sha_file = (folder / f"{bundle.name}.sha256").read_text().split()[0]
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
                     "setup/upgrade.sh", "setup/make_release.sh", "setup/send_to_device.sh",
                     "setup/usb/"):
            check(f"left out: {gone}", not any(p == gone.rstrip("/") or p.startswith(gone)
                                              for p in rel))
        check("no secrets file inside", not any("secrets.env" in p and "example" not in p
                                                for p in rel))
        check("the device's missing system packages ride along (debs/, with their list)",
              {"debs/" + pkg.name, "debs/debs.txt"} <= rel, str(sorted(p for p in rel
                                                                   if p.startswith("debs"))))
    pkg.write_bytes(b"a CHANGED package")                       # not what the device listed
    shutil.rmtree(pc / "debs", ignore_errors=True)
    bad = release()
    check("a package that does not match the device's checksum stops the build",
          bad.returncode != 0 and "checksum" in bad.stderr, bad.stderr[-160:])
else:
    print("  (git not available — bundle checks skipped)")

print("\n6. the secrets file is read as data")
src = (ROOT / "setup/install.sh").read_text(encoding="utf-8")
fn = re.search(r"^SECRET_KEYS=.*?^read_secrets\(\) \{.*?^\}\n", src, re.S | re.M).group(0)
sec = out / "s.env"
sec.write_bytes(b'# comment\r\nCERAVIS_API_KEY = "k1"\r\nFRP_TOKEN=\'t 2\'\n'
                b'FRP_SERVER_ADDR=1.2.3.4   \nNOT_A_KEY=x\n'
                b'REBOOT_PASSWORD=p$(touch never)w\nFMS_ENROLL_KEY=last-line-no-newline')
r = sh(fn + f'read_secrets "{posix(sec)}"; echo "$CERAVIS_API_KEY|$FRP_TOKEN|$FRP_SERVER_ADDR|'
       '${NOT_A_KEY:-}|$REBOOT_PASSWORD|$FMS_ENROLL_KEY"')
got = r.stdout.strip().split("|")
check("spaces around '=', quotes and CRLF handled", got[:3] == ["k1", "t 2", "1.2.3.4"], r.stdout)
check("only the known keys are taken", len(got) > 3 and got[3] == "")
check("nothing in a value is executed", len(got) > 4 and got[4] == "p$(touch never)w"
      and not (ROOT / "never").exists())
check("the last line without a newline is read (the frp key is never lost)",
      len(got) > 5 and got[5] == "last-line-no-newline")

print("\n7. the tunnel config survives a re-run with the same server")
frpc = (ROOT / "cloud/install_frpc.sh").read_text(encoding="utf-8")
fn = re.search(r"^same_server\(\) \{.*?^\}\n", frpc, re.S | re.M).group(0)
keyed = out / "frpc-keyed.toml"
keyed.write_text((ROOT / "cloud/frpc.toml.example").read_text()
                 .replace("EC2_PUBLIC_IP_OR_DOMAIN", "1.2.3.4")
                 .replace("CHANGE_ME_TO_A_LONG_RANDOM_SECRET", "tok"))
probe = fn + f'CONF="{posix(keyed)}"; same_server && echo same || echo other'
check("same server and token -> kept",
      sh(probe, {"FRP_SERVER_ADDR": "1.2.3.4", "FRP_TOKEN": "tok"}).stdout.strip() == "same")
check("a new token -> rebuilt",
      sh(probe, {"FRP_SERVER_ADDR": "1.2.3.4", "FRP_TOKEN": "new"}).stdout.strip() == "other")

shutil.rmtree(out, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All installer checks passed.")
