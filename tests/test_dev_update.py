"""
setup/dev_update.sh — pull the code, restart only what changed, never the fleet side.

Runs the real script with bash and git: a scratch "origin" and a clone of it (the
bench), stand-ins for sudo and curl that record what they were asked to do.

1. pages only (edge/static): pulled, nothing restarted
2. app code: pulled, ceravis restarted — and nothing else (no fleet agent, no
   enrollment, no units, no apt)
3. fleet agent code: pulled, the agent is NOT restarted (it restarts itself)
4. units / setup files: named for the full upgrade, not applied
5. --dry-run changes nothing; already up to date says so

    python tests/test_dev_update.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail[:300]) if detail else ''}")
    if not ok:
        failures.append(label)


def find_bash():
    """A real bash. On Windows the `bash` on PATH is usually WSL's launcher, so take Git's."""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    for folder in pathlib.Path(git).resolve().parents if git else ():
        if (folder / "bin" / "bash.exe").exists():
            return str(folder / "bin" / "bash.exe")
    return None


def write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    path.chmod(0o755)


BASH = find_bash()
if BASH is None or not shutil.which("git"):
    print("skipped: needs bash and git")
    sys.exit(0)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
    tmp = pathlib.Path(scratch)
    (tmp / "gitconfig").write_text("")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
           # no background `git gc`/maintenance: a detached child would hold the pipe open
           "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false",
           "GIT_CONFIG_KEY_1": "gc.auto", "GIT_CONFIG_VALUE_1": "0",
           "GIT_CONFIG_KEY_2": "maintenance.auto", "GIT_CONFIG_VALUE_2": "false",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    origin, dev, bench, stubs = tmp / "origin.git", tmp / "dev", tmp / "bench", tmp / "stubs"
    calls = tmp / "calls.txt"

    def git(*args, cwd=tmp):
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout

    # sudo and curl stand-ins: record every privileged command; the app "answers".
    write(stubs / "sudo", f'#!/bin/sh\necho "sudo $*" >> "{calls.as_posix()}"\n')
    write(stubs / "curl", '#!/bin/sh\necho \'{"version":"1.3.0","code":{"commit":"abc1234"}}\'\n')
    # The stand-ins go first INSIDE bash: Git for Windows' bash puts its own
    # /mingw64/bin (a real curl) ahead of the PATH it is given.
    run_env = {**env, "STUBS": str(stubs)}
    STUBBED = ('S="$STUBS"; command -v cygpath >/dev/null && S="$(cygpath -u "$STUBS")"; '
               'PATH="$S:$PATH" exec bash setup/dev_update.sh "$@"')

    git("init", "-q", "--bare", "-b", "dev_ceravis1.1", str(origin))
    git("clone", "-q", str(origin), str(dev))
    git("switch", "-q", "-c", "dev_ceravis1.1", cwd=dev)
    write(dev / "setup" / "dev_update.sh", (ROOT / "setup" / "dev_update.sh").read_text(encoding="utf-8"))
    write(dev / "edge" / "main.py", "x = 1\n")
    write(dev / "edge" / "static" / "page.html", "<p>1</p>\n")
    write(dev / "edge" / "fleet" / "fms_agent" / "agent.py", "a = 1\n")
    git("add", "-A", cwd=dev)
    git("commit", "-q", "-m", "start", cwd=dev)
    git("push", "-q", "origin", "dev_ceravis1.1", cwd=dev)
    git("clone", "-q", "-b", "dev_ceravis1.1", str(origin), str(bench))

    def publish(path: str, text: str) -> None:
        write(dev / path, text)
        git("add", "-A", cwd=dev)
        git("commit", "-q", "-m", f"change {path}", cwd=dev)
        git("push", "-q", "origin", "dev_ceravis1.1", cwd=dev)

    def update(*args) -> tuple[str, str]:
        calls.write_text("")
        log = tmp / "out.txt"
        with open(log, "w") as fh:          # a file, not a pipe: no child can hold it open
            subprocess.run([BASH, "-c", STUBBED, "dev_update", *args], cwd=bench, env=run_env,
                           stdout=fh, stderr=subprocess.STDOUT, timeout=60)
        return log.read_text(), calls.read_text()

    print("\n1. pages only")
    publish("edge/static/page.html", "<p>2</p>\n")
    out, did = update()
    check("pulled, nothing restarted, told to reload the page",
          (bench / "edge/static/page.html").read_text() == "<p>2</p>\n" and did == ""
          and "reload them in the browser" in out, out)

    print("\n2. app code")
    publish("edge/main.py", "x = 2\n")
    out, did = update()
    check("pulled; ceravis restarted and NOTHING else asked of sudo",
          (bench / "edge/main.py").read_text() == "x = 2\n"
          and did.strip().splitlines() == ["sudo systemctl restart ceravis"], did)
    check("...and it says the app answers again with its version and commit",
          'the app answers again: {"version":"1.3.0","code":{"commit":"abc1234"}}' in out, out)
    check("never the fleet side: no agent, enrollment, edge_id, unit or apt step",
          not any(w in did for w in ("fleet", "enroll", "edge_id", "daemon-reload", "apt", "install")), did)

    print("\n3. fleet agent code")
    publish("edge/fleet/fms_agent/agent.py", "a = 2\n")
    out, did = update()
    check("pulled; the agent is left to restart itself (no sudo at all)",
          (bench / "edge/fleet/fms_agent/agent.py").read_text() == "a = 2\n" and did == ""
          and "restarts itself" in out, out)

    print("\n4. units and setup")
    publish("edge/infra/systemd/ceravis.service", "[Service]\n")
    out, did = update()
    check("named for the full upgrade, not applied (no daemon-reload, no install)",
          "edge/infra/systemd/ceravis.service" in out and "setup/upgrade.sh" in out and did == "", out)

    print("\n5. dry run and up to date")
    publish("edge/main.py", "x = 3\n")
    out, did = update("--dry-run")
    check("--dry-run: says it would restart ceravis, pulls nothing, runs nothing",
          "restart ceravis: yes" in out and (bench / "edge/main.py").read_text() == "x = 2\n" and did == "", out)
    update()
    out, did = update()
    check("already up to date: says so, does nothing", "Already up to date" in out and did == "", out)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All dev-update checks passed.")
