#!/usr/bin/env python3
"""
setup/patch_device.sh — patching a few files of an installed release.

Run against a stand-in install folder with stand-ins for sudo and systemctl;
the "GitHub" is a local folder served through curl's file:// URLs.

Covered:
  files equal to FROM are replaced by TO, backed up, the service restarted and
  /health waited for;
  a file that is neither FROM nor TO (hand-edited, another release) stops it
  before ANY file is changed;
  running it again is a no-op ("already patched"), no restart;
  ROLLBACK.sh puts the old files back;
  a file not in the release is refused (that is a release, not a patch);
  a branch name instead of a hash and a path leaving the repo are refused;
  a service that never answers /health is reported (exit 2) with the undo.

    python tests/test_patch_device.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "setup" / "patch_device.sh"
# shutil.which, not a bare "bash": on Windows CreateProcess would find System32's
# WSL launcher before Git Bash.
BASH = shutil.which("bash")
FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)


if not BASH:
    print("bash not found — skipped")
    sys.exit(0)

TMP = Path(tempfile.mkdtemp(prefix="ceravis-patch-"))
RAW, APP, BK, BIN = TMP / "raw", TMP / "app", TMP / "backups", TMP / "bin"
OLD, NEW = "aaaaaaa", "bbbbbbb"
FILES = {"edge/api/x.py": ("old x\n", "new x\n"),
         "edge/static/page.html": ("<p>old</p>\r\n", "<p>new</p>\r\n")}
for f, (a, b) in FILES.items():
    for ref, text in ((OLD, a), (NEW, b)):
        p = RAW / ref / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
(RAW / "health").write_text("{\"status\": \"ok\"}")

BIN.mkdir()
(BIN / "sudo").write_text('#!/usr/bin/env bash\nexec "$@"\n')
(BIN / "systemctl").write_text(f'#!/usr/bin/env bash\necho "$@" >> "{TMP.as_posix()}/systemctl.log"\n')
# Windows cannot hold a Linux owner: chown is a stand-in (chmod is the real one).
(BIN / "chown").write_text('#!/usr/bin/env bash\nexit 0\n')
for b in BIN.iterdir():
    b.chmod(0o755)


def reset_app() -> None:
    shutil.rmtree(APP, ignore_errors=True)
    shutil.rmtree(BK, ignore_errors=True)
    (TMP / "systemctl.log").unlink(missing_ok=True)
    for f, (a, _b) in FILES.items():
        p = APP / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(a.encode())


def run(*args: str, health: str = "health") -> subprocess.CompletedProcess:
    env = {**os.environ,
           "PATH": f"{BIN}{os.pathsep}{os.environ['PATH']}",
           "CERAVIS_RAW": f"file://{RAW.as_posix()}",
           "CERAVIS_APP": APP.as_posix(), "CERAVIS_BACKUPS": BK.as_posix(),
           "CERAVIS_HEALTH": f"file://{(RAW / health).as_posix()}",
           "CERAVIS_HEALTH_SECS": "3"}
    return subprocess.run([BASH, SCRIPT.as_posix(), *args], env=env,
                          capture_output=True, text=True, encoding="utf-8")


def app_text(f: str) -> bytes:
    return (APP / f).read_bytes()


def restarts() -> int:
    log = TMP / "systemctl.log"
    return log.read_text().count("restart") if log.exists() else 0


files = list(FILES)

print("\na device on FROM is patched to TO")
reset_app()
r = run(OLD, NEW, *files)
check("exit 0 and says OK", r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr)
check("every file now has the TO bytes (CRLF kept exactly)",
      all(app_text(f) == FILES[f][1].encode() for f in files))
check("the service was restarted once", restarts() == 1)
bk = next(BK.iterdir())
check("no temporary file is left beside the patched ones",
      not list(APP.rglob("*.patch-new")))
check("the old files are backed up", sorted(p.name for p in bk.iterdir())
      == sorted(["PATCH", "ROLLBACK.sh"] + [f.replace("/", "__") for f in files]))

print("\nrunning it again changes nothing")
r = run(OLD, NEW, *files)
check("already patched: exit 0, no restart, no new backup",
      r.returncode == 0 and "already has the patch" in r.stdout
      and restarts() == 1 and len(list(BK.iterdir())) == 1, r.stdout)

print("\nundo")
r = subprocess.run([BASH, (bk / "ROLLBACK.sh").as_posix()], capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{BIN}{os.pathsep}{os.environ['PATH']}"})
check("ROLLBACK.sh restores the FROM files and restarts",
      r.returncode == 0 and all(app_text(f) == FILES[f][0].encode() for f in files)
      and restarts() == 2, r.stdout + r.stderr)

print("\na release whose text file has other line endings is still recognised")
reset_app()
# What the 1.3.0 build (Windows, autocrlf) made of a CRLF file: \r\r\n.
(APP / files[1]).write_bytes(FILES[files[1]][0].replace("\r\n", "\r\r\n").encode())
r = run(OLD, NEW, *files)
check("patched, and the file now has exactly the TO bytes",
      r.returncode == 0 and app_text(files[1]) == FILES[files[1]][1].encode(), r.stderr)

print("\nan unexpected file stops everything before any change")
reset_app()
(APP / files[1]).write_bytes(b"edited by hand\n")
r = run(OLD, NEW, *files)
check("refused, names the file", r.returncode == 1 and files[1] in r.stderr, r.stderr)
check("not even the expected file was changed, no restart, no backup",
      app_text(files[0]) == FILES[files[0]][0].encode() and restarts() == 0
      and not BK.exists())

print("\nrefusals")
reset_app()
r = run(OLD, NEW, "edge/new_module.py")
check("a file the release does not have is refused", r.returncode == 1
      and "does not exist" in r.stderr, r.stderr)
check("a branch name instead of a hash is refused",
      run("prod_ceravis1.1", NEW, files[0]).returncode == 1)
check("a path leaving the repo is refused",
      run(OLD, NEW, "../etc/passwd").returncode == 1
      and run(OLD, NEW, "/etc/passwd").returncode == 1)
check("too few arguments is refused", run(OLD, NEW).returncode == 1)
check("nothing was changed by any refusal",
      all(app_text(f) == FILES[f][0].encode() for f in files) and restarts() == 0)

print("\na service that does not come back is reported")
reset_app()
r = run(OLD, NEW, *files, health="no-such-health")
check("exit 2, with how to look and how to undo",
      r.returncode == 2 and "journalctl" in r.stderr and "ROLLBACK.sh" in r.stderr,
      r.stderr)

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All patch_device checks passed.")
