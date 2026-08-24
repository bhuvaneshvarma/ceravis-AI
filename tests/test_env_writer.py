"""
The runtime env file must be ONE file, and it must not be tracked.

env_writer persists EDGE_ID into edge/infra/env/jetson.env at account
verification. That file is therefore GITIGNORED and generated from
jetson.env.example by setup/setup.sh — a TRACKED file that the device rewrites
leaves every unit with a dirty working tree, so the next commit touching it
makes `git pull` abort, and the usual escape (`git checkout --`) silently
discards the edge_id: the fleet routing token AND the control-API credential.

Run:  PYTHONPATH=edge python tests/test_env_writer.py
"""
import io
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "edge"))

from config.env_writer import set_env_value      # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = "edge/infra/env/jetson.env"
EXAMPLE = ROOT / "edge/infra/env/jetson.env.example"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("\n1. the live env file is NOT tracked, and a template IS")
tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ENV],
                         cwd=ROOT, capture_output=True, text=True).returncode == 0
check("jetson.env is untracked", not tracked)
ignored = subprocess.run(["git", "check-ignore", ENV],
                         cwd=ROOT, capture_output=True, text=True).returncode == 0
check("jetson.env is gitignored", ignored)
check("jetson.env.example exists as the template", EXAMPLE.exists())
tracked_ex = subprocess.run(["git", "ls-files", "--error-unmatch",
                             "edge/infra/env/jetson.env.example"],
                            cwd=ROOT, capture_output=True, text=True).returncode == 0
check("the template IS tracked", tracked_ex)


print("\n2. the template ships EDGE_ID blank (it is per-device)")
text = io.open(EXAMPLE, encoding="utf-8").read()
lines = [l for l in text.splitlines()
         if l.startswith("EDGE_ID=") and not l.startswith("#")]
check("exactly one EDGE_ID line", len(lines) == 1, str(lines))
check("and it is empty", lines == ["EDGE_ID="], str(lines))


print("\n3. set_env_value updates in place and preserves everything else")
tmp = pathlib.Path(tempfile.mkdtemp()) / "jetson.env"
tmp.write_text("# header\nEDGE_ID=\nRECORD_SEGMENT_SECS=15\n# trailing\n",
               encoding="utf-8")
set_env_value("EDGE_ID", "NrPabc123", env_file=tmp)
out = tmp.read_text(encoding="utf-8")
check("value written", "EDGE_ID=NrPabc123" in out)
check("other keys untouched", "RECORD_SEGMENT_SECS=15" in out)
check("comments preserved", "# header" in out and "# trailing" in out)
check("no duplicate key", out.count("EDGE_ID=") == 1, out)

set_env_value("EDGE_ID", "NrPsecond", env_file=tmp)
out = tmp.read_text(encoding="utf-8")
check("re-write replaces, never appends", out.count("EDGE_ID=") == 1
      and "NrPsecond" in out and "NrPabc123" not in out)

set_env_value("BRAND_NEW_KEY", "7", env_file=tmp)
check("an absent key is appended",
      "BRAND_NEW_KEY=7" in tmp.read_text(encoding="utf-8"))


print("\n4. a missing file is created, not crashed on")
fresh = pathlib.Path(tempfile.mkdtemp()) / "nested" / "jetson.env"
check("write to a missing path succeeds", set_env_value("EDGE_ID", "x1", env_file=fresh))
check("file now exists with the value",
      fresh.exists() and "EDGE_ID=x1" in fresh.read_text(encoding="utf-8"))


print("\n5. jetson.local.env is gone from the tree")
hits = []
for p in ROOT.rglob("*"):
    if p.is_dir() or ".git" in p.parts or "__pycache__" in p.parts:
        continue
    if p.name == "test_env_writer.py":
        continue                      # this file names the string to ban it
    if p.suffix in (".py", ".sh", ".env", ".example", ".md", ".service", ".timer"):
        try:
            if "jetson.local.env" in io.open(p, encoding="utf-8", errors="replace").read():
                hits.append(p.relative_to(ROOT).as_posix())
        except OSError:
            pass
check("no jetson.local.env references remain", not hits, ", ".join(hits[:5]))


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll env-writer checks passed.")
