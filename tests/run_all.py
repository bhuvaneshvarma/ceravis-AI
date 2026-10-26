#!/usr/bin/env python3
"""
Run every test in this folder — the one command for the whole suite.

    python tests/run_all.py            # all of them
    python tests/run_all.py auth otp   # only files whose name contains a word

Each tests/test_*.py runs as its own process (they are standalone scripts with
their own checks), with edge/ on the path, UTF-8 output, and its OWN empty
temporary DATA_DIR — so no test can ever write into a real device's data.
Files not named test_*.py (e.g. manual_fire_fall_alert.py, which sends a REAL
alert) are never run. Exit code 0 = everything passed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main(words: list[str]) -> int:
    tests = sorted(p for p in HERE.glob("test_*.py")
                   if not words or any(w in p.stem for w in words))
    if not tests:
        print("no tests match", words)
        return 1
    failed = []
    t_all = time.monotonic()
    for path in tests:
        data = tempfile.mkdtemp(prefix="ceravis-test-data-")
        # No device layout file either (/etc/ceravis/layout.env on a device):
        # every test sees the same edge/ defaults wherever it runs.
        env = {**os.environ, "PYTHONPATH": str(ROOT / "edge"), "DATA_DIR": data,
               "CERAVIS_LAYOUT_FILE": os.devnull,
               "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        t = time.monotonic()
        try:
            proc = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=600)
            ok, out = proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            ok, out = False, "timed out after 600 s"
        finally:
            shutil.rmtree(data, ignore_errors=True)
        last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
        print(f"{'ok  ' if ok else 'FAIL'}  {time.monotonic() - t:5.1f}s  {path.name}  — {last[:90]}")
        if not ok:
            failed.append(path.name)
            print("      " + "\n      ".join(out.splitlines()[-15:]))
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed in {time.monotonic() - t_all:.0f}s")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
