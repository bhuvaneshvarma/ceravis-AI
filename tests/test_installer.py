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
8. lessons from the first real device: the NVIDIA hold, the offline .deb files,
   the uplink, the hotspot taken last, the seal removing delivered folders
9. send_to_device.sh --code puts the committed code in ~/CERAVIS/patch and installs again
10. --package: ONE file to download — the folder, the keys encrypted (opened only
   with the passphrase, by the folder installer's own cipher setting), SSH keys

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
    print("\n10. ONE file to download (--package)")
    (pc / "authorized_keys").write_text("ssh-ed25519 AAAA developer\n")
    r = release("--package")
    packed = dist / f"CERAVIS-{version}.tar"
    check("packed as ONE file with its fingerprint", r.returncode == 0 and packed.exists()
          and (dist / f"{packed.name}.sha256").read_text().split()[0]
          == hashlib.sha256(packed.read_bytes()).hexdigest(), (r.stderr or r.stdout)[-200:])
    if packed.exists():
        with tarfile.open(packed) as tf:
            names = {n.rstrip("/") for n in tf.getnames()}
            enc = tf.extractfile("CERAVIS/ceravis-secrets.env.enc").read()
        check("it holds the folder, the keys encrypted and the SSH public keys",
              {"CERAVIS/install.sh", "CERAVIS/README.txt", f"CERAVIS/{bundle.name}",
               f"CERAVIS/{bundle.name}.sha256", "CERAVIS/ceravis-secrets.env.enc",
               "CERAVIS/authorized_keys"} <= names, str(sorted(names)))
        check("…and no key readable anywhere in it", "CERAVIS/ceravis-secrets.env" not in names
              and b"never-in-the-folder" not in packed.read_bytes())
        phrase = (pc / f"CERAVIS-{version}.passphrase").read_text().strip()
        check("the passphrase: 16 characters nobody mishears, in groups of 4",
              re.fullmatch(r"([A-HJ-NP-Z2-9]{4}-){3}[A-HJ-NP-Z2-9]{4}", phrase) is not None, phrase)
        check("…shown at the build", phrase in r.stdout)
        cipher = re.search(r"^CIPHER=\((.*)\)$", (ROOT / "setup/usb/install.sh")
                           .read_text(encoding="utf-8"), re.M).group(1)
        (out / "s.enc").write_bytes(enc)
        dec = f'openssl enc -d {cipher} -pass stdin -in "{posix(out / "s.enc")}"'
        opened = sh(f'printf "%s\\n" "{phrase}" | {dec}')
        check("the folder installer's own setting opens them with that passphrase",
              opened.stdout.strip() == "CERAVIS_API_KEY=never-in-the-folder", opened.stderr[-120:])
        check("…and refuses any other", sh(f'printf "wrong\\n" | {dec}').returncode != 0)
        # The folder installer's own passphrase loop, each typed answer from a list.
        usb = (ROOT / "setup/usb/install.sh").read_text(encoding="utf-8")
        loop = re.search(r"^# Encrypted secrets: the passphrase.*?^fi\n", usb, re.S | re.M).group(0)
        here = out / "here"
        here.mkdir()

        def typed(answers: list[str], enc_bytes: bytes = enc) -> subprocess.CompletedProcess:
            (here / "ceravis-secrets.env.enc").write_bytes(enc_bytes)
            (out / "tries").write_text("\n".join(answers) + "\n")
            (out / "n").write_text("0")
            (here / "opened").unlink(missing_ok=True)
            return sh(f'T="{posix(out)}"; HERE="{posix(here)}"; OPENED="$HERE/opened"\n'
                      f'CIPHER=({cipher}); say() {{ echo "== $*"; }}\n'
                      'openssl() { local n; n=$(cat "$T/n"); echo $((n + 1)) > "$T/n"; '
                      'command openssl "$@" -pass "pass:$(sed -n "$((n + 1))p" "$T/tries")"; }\n'
                      + loop + 'echo "tries=$(cat "$T/n")"; cat "$OPENED"')

        r = typed(["wrong", phrase])
        check("a wrong passphrase -> asked again; the right one opens the keys",
              "tries=2" in r.stdout and "CERAVIS_API_KEY=never-in-the-folder" in r.stdout
              and "try again" in r.stdout, r.stdout[-160:] + r.stderr[-120:])
        r = typed(["a", "b", "c", phrase])
        check("three wrong -> stops, nothing opened, nothing installed",
              r.returncode != 0 and "nothing was installed" in r.stderr
              and not (here / "opened").exists(), r.stderr[-120:])
        noise = sh(f'printf "random words\\n" | openssl enc {cipher} -salt -pass pass:{phrase} '
                   f'-out "{posix(out / "noise.enc")}"')
        r = typed([phrase] * 3, (out / "noise.enc").read_bytes() if noise.returncode == 0 else b"")
        check("a file that opens to something without the keys counts as not opened",
              r.returncode != 0 and not (here / "opened").exists(), r.stdout[-120:])
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
sec.write_bytes(b"HOTSPOT_PASSWORD = fleet-wide-pass1\r\n")
r = sh(fn + f'read_secrets "{posix(sec)}"; echo "$HOTSPOT_PASSWORD"')
check("the fleet's hotspot password is a secrets-file key", r.stdout.strip() == "fleet-wide-pass1",
      r.stdout + r.stderr[-100:])
check("…stored root-only, checked 8-63 characters before anything changes",
      'printf \'%s\\n\' "$HOTSPOT_PASSWORD" > "$CERAVIS_CREDSTORE/hotspot_password"' in src
      and "HOTSPOT_PASSWORD must be 8 to 63 characters" in src)

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

print("\n8. lessons from the first real device (2026-10-27)")
tune = (ROOT / "setup/tune_os.sh").read_text(encoding="utf-8")
awk = re.search(r"\| awk '([^']+)'", tune).group(1)
r = sh("printf 'ii  nvidia-l4t-core\\nun  libcudnn9\\npn  libnvinfer-doc\\nhi  libnvinfer10\\n"
       f"rc  nvidia-l4t-ccp-generic\\n' | awk '{awk}' | sort | tr '\\n' ' '")
check("NVIDIA hold: only installed packages (ii, hi) — apt-mark refuses the rest",
      r.stdout.split() == ["libnvinfer10", "nvidia-l4t-core"], r.stdout)
check("…and a refused hold never stops the install", "|| echo" in tune.split("apt-mark hold")[1][:120])
native = (ROOT / "setup/install_native.sh").read_text(encoding="utf-8")
deb_cmd = next(ln for ln in native.splitlines() if "apt_get install" in ln and "NEED" in ln)
check("the release's .deb files install without --no-download (it refuses local files)",
      "--no-download" not in deb_cmd, deb_cmd.strip())
need = re.search(r"^    NEED=\(\)\n.*?^    done\n", native, re.S | re.M).group(0)
debdir = out / "debs"
debdir.mkdir()
for name in ("newpkg_1.0_arm64.deb", "libold_1.5_arm64.deb", "libsame_1.0_arm64.deb",
             "libup_1.0_arm64.deb"):
    (debdir / name).write_bytes(b"deb")
r = sh('dpkg-deb() { local b="${2##*/}"; b="${b%_arm64.deb}"; '
       'if [ "$3" = Package ]; then echo "${b%%_*}"; else echo "${b#*_}"; fi; }\n'
       'dpkg-query() { case "$3" in libold) printf "install ok installed 2.0" ;; '
       'libsame) printf "install ok installed 1.0" ;; libup) printf "install ok installed 0.9" ;; '
       '*) return 1 ;; esac; }\n'
       'dpkg() { [ "$(printf "%s\\n%s\\n" "$2" "$4" | sort -V | head -1)" = "$4" ]; }\n'
       f'DEBS="{posix(debdir)}"\n' + need + 'for f in "${NEED[@]}"; do echo "${f##*/}"; done')
check("a .deb whose package is already there at the same or a newer version is left out "
      "(apt refuses a downgrade)", r.stdout.split() == ["libup_1.0_arm64.deb", "newpkg_1.0_arm64.deb"],
      r.stdout + r.stderr[-120:])
check("apt waits for a running apt (a fresh system's daily update) instead of failing",
      "DPkg::Lock::Timeout" in (ROOT / "setup/lib.sh").read_text(encoding="utf-8")
      and "sudo apt-get" not in native)
inst = (ROOT / "setup/install.sh").read_text(encoding="utf-8")
sed_dev = re.search(r"sed -n '(s/\.\* dev [^']+)'", inst).group(1)
r = sh("printf '1.1.1.1 via 192.168.0.1 dev wlP1p1s0 src 192.168.0.164 uid 0 \\n    cache \\n' "
       f"| sed -n '{sed_dev}' | head -1")
check("the uplink device is read from the route (Wi-Fi or Ethernet)", r.stdout.strip() == "wlP1p1s0",
      r.stdout)
check("the install's first-boot step leaves the hotspot to the end",
      "CERAVIS_DEFER_HOTSPOT=1 bash" in inst
      and 'CERAVIS_DEFER_HOTSPOT:-0}" = 1' in (ROOT / "setup/firstboot.sh").read_text(encoding="utf-8"))
end = inst[inst.index('stage "9/9"'):]
check("…and takes the radio only after every service is installed",
      end.index("common.hotspot ensure") > end.index("systemctl restart ceravis"))
seal = (ROOT / "setup/seal.sh").read_text(encoding="utf-8")
check("the seal removes delivered release folders (they hold the keys)",
      "rm -rf /home/*/CERAVIS" in seal and "ceravis-install.result" in seal)
check("…and downloaded release files", "/home/*/Downloads/CERAVIS-*.tar*" in seal)
usb_src = (ROOT / "setup/usb/install.sh").read_text(encoding="utf-8")
fns = "".join(re.search(rf"^{n}\(\) \{{.*?^\}}\n", usb_src, re.S | re.M).group(0)
              for n in ("status", "follow"))
(out / "result").write_text("ok 2026-10-27T13:17:46+05:30\n")
r = sh('systemctl() { return 3; }; say() { echo "== $*"; }\n'
       f'HERE=/home/ceravis/CERAVIS; UNIT=ceravis-install; RESULT="{posix(out / "result")}"; LOG=/x\n'
       + fns + 'follow; echo "exit=$?"')
check("--follow with nothing installing says so — never an old log that reads like news "
      "(a copied release once sat uninstalled behind this morning's log)",
      "nothing is installing now" in r.stdout and "sudo bash /home/ceravis/CERAVIS/install.sh" in r.stdout
      and "following the install" not in r.stdout, r.stdout[-200:])
check("a new install starts a new log (the last kept as .1), before it starts — "
      "its progress never opens with the previous install's ending",
      '[ -s "$LOG" ] && mv -f "$LOG" "$LOG.1"' in usb_src
      and usb_src.index('mv -f "$LOG" "$LOG.1"') < usb_src.index("systemd-run --unit"))
send = (ROOT / "setup/send_to_device.sh").read_text(encoding="utf-8")
check("a connection closed at step 3 says how to start it without copying again (--start)",
      "--start) MODE=start" in send and '[ "$rc" = 255 ]' in send
      and 'start) on "sudo bash ~/CERAVIS/install.sh"' in send)

print("\n9. --code: a fix reaches a device in seconds")
if shutil.which("git"):
    dev = out / "device"
    (dev / "CERAVIS").mkdir(parents=True)
    (dev / "CERAVIS" / "ceravis-edge-1.3.0.tar.gz").write_bytes(b"the release already there")
    fake = out / "fakebin"
    fake.mkdir()
    (fake / "ssh").write_text(
        "#!/bin/bash\nfor a in \"$@\"; do last=\"$a\"; done\n"
        f"case \"$last\" in *ceravis-edge-*) HOME='{posix(dev)}' bash -c \"$last\" ;;\n"
        "  *) echo \"ON DEVICE: $last\" ;; esac\n")
    os.chmod(fake / "ssh", 0o755)
    r = subprocess.run([BASH, posix(ROOT / "setup/send_to_device.sh"), "ceravis@test", "--code"],
                       cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PATH": posix(fake) + os.pathsep + os.environ.get("PATH", "")})
    patch = dev / "CERAVIS" / "patch"
    check("the committed code lands in ~/CERAVIS/patch", (patch / "edge" / "main.py").exists()
          and (patch / "setup" / "install.sh").exists(), (r.stderr or r.stdout)[-160:])
    check("…without tests or dev tools", not (patch / "tests").exists()
          and not (patch / "setup" / "make_release.sh").exists())
    check("the folder installer is refreshed", (dev / "CERAVIS" / "install.sh").read_text(
        encoding="utf-8").startswith("#!/usr/bin/env bash"))
    check("the release already there is kept", (dev / "CERAVIS" / "ceravis-edge-1.3.0.tar.gz")
          .read_bytes() == b"the release already there")
    check("then the install runs again", "ON DEVICE: sudo bash ~/CERAVIS/install.sh" in r.stdout)
    (dev / "CERAVIS" / "ceravis-edge-1.3.0.tar.gz").unlink()
    r = subprocess.run([BASH, posix(ROOT / "setup/send_to_device.sh"), "ceravis@test", "--code"],
                       cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PATH": posix(fake) + os.pathsep + os.environ.get("PATH", "")})
    check("no release on the device -> refused, nothing installed",
          r.returncode != 0 and "send the full release first" in r.stderr
          and "ON DEVICE" not in r.stdout, r.stderr[-120:])

shutil.rmtree(out, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All installer checks passed.")
