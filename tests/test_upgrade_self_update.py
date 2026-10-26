"""
setup/upgrade.sh must run the steps its OWN pull brings.

bash goes on reading the copy of a script it started with. When upgrade.sh
pulled a new version of itself, the rest of that upgrade still ran the OLD
steps — so a step added to it (the fleet agent's install) reached a device one
upgrade late. It now starts again on the pulled copy, once.

This runs the real upgrade.sh with bash and git in a scratch repository: a
"device" clone runs it, and its pull brings a copy with one extra step. The
scripts it calls, and sudo / pip3 / python3 / sleep / rm / hostname, are stand-ins:
nothing is installed, built, restarted or deleted.

Run:  python tests/test_upgrade_self_update.py
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def find_bash():
    """A real bash. On Windows the `bash` on PATH is usually WSL's launcher, so take Git's."""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")                         # Git\cmd\git.exe or Git\mingw64\bin\git.exe
    for folder in pathlib.Path(git).resolve().parents if git else ():
        if (folder / "bin" / "bash.exe").exists():
            return str(folder / "bin" / "bash.exe")
    return None


def write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))            # LF as written: bash rejects CRLF
    path.chmod(0o755)


BASH = find_bash()
if BASH is None or not shutil.which("git"):
    print("skipped: needs bash and git")
    sys.exit(0)

script = (ROOT / "setup" / "upgrade.sh").read_text(encoding="utf-8")
STEP_2 = 'echo "== [2/6]'
newer = script.replace(STEP_2, 'echo "== a step the pull brought =="\n' + STEP_2, 1)
called = sorted(set(re.findall(r'"\$SETUP_DIR/([\w.-]+\.sh)"', script)) - {"upgrade.sh"})

print("\nupgrade.sh after a pull that changes it")
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
    tmp = pathlib.Path(scratch)
    (tmp / "gitconfig").write_text("")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    dev, device, stubs = tmp / "dev", tmp / "device", tmp / "stubs"

    def git(*args, cwd=tmp):
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    def publish(upgrade: str) -> None:
        write(dev / "setup" / "upgrade.sh", upgrade)
        for name in called:
            # lib.sh is sourced, not run: its stand-in defines what upgrade.sh calls.
            extra = 'write_layout() { echo "ran write_layout"; }\n' if name == "lib.sh" else ""
            write(dev / "setup" / name, f'#!/usr/bin/env bash\necho "ran {name}"\n{extra}')
        write(dev / "edge" / "README", "edge\n")
        git("add", "-A", cwd=dev)
        git("commit", "-qm", "upgrade", cwd=dev)
        git("push", "-q", "origin", "HEAD:main", cwd=dev)

    git("init", "-q", "--bare", "-b", "main", "origin.git")
    git("clone", "-q", "origin.git", "dev")
    publish(script)                                   # what the device runs today
    git("clone", "-q", "origin.git", "device")
    publish(newer)                                    # what its upgrade pulls
    for name in ("sudo", "pip3", "python3", "sleep", "rm", "hostname"):   # Git Bash's hostname has no -I
        write(stubs / name, "#!/bin/sh\nexit 0\n")

    run = subprocess.run([BASH, "-c", 'cd "$1" && PATH="$PWD:$PATH" exec bash "$2"', "_",
                          str(stubs), (device / "setup" / "upgrade.sh").as_posix()],
                         env=env, capture_output=True, encoding="utf-8", errors="replace",
                         timeout=120)
    out = run.stdout
    check("a step can be added before step 2", newer != script)
    check("the upgrade finished", run.returncode == 0,
          (run.stdout + run.stderr)[-600:] if run.returncode else "")
    check("the step its pull brought ran on THIS upgrade", "== a step the pull brought ==" in out)
    check("it pulled once (the new copy does not pull again)",
          out.count("== [1/6] latest code ==") == 1, f"{out.count('== [1/6] latest code ==')}x")
    check("the fleet agent install ran", "ran install_fleet_agent.sh" in out)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll upgrade self-update checks passed.")
