#!/usr/bin/env python3
"""
Prove start-up resumes an INTERRUPTED enrollment but never re-enables one that
was switched off.

Removing a finished recipient's embeddings (body/) is how the AI chain is
switched off today — 2026-09-23 on the bench, with several people in the room
and a wrong target raising false events. An earlier version rebuilt that
gallery from the stored photos at start-up, which would have switched the AI
straight back on. Resuming is only for enrollments that were committed and
never finished (queued / processing / pending_reid / error).

Covered:
  a 'ready' recipient whose embeddings were removed stays OFF (not rebuilt);
  a committed enrollment that never finished IS resumed;
  a 'ready' recipient with embeddings is left alone;
  a draft (media added, never committed) is not auto-enrolled.

Pure python; no TensorRT. Runs on the dev box:

    python tests/test_enroll_resume.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

_TMP = Path(tempfile.mkdtemp(prefix="ceravis-enroll-"))
os.environ["DATA_DIR"] = str(_TMP)

from enrollment.enrollment_manager import EnrollmentManager   # noqa: E402
from enrollment.enrollment_worker import EnrollmentWorker     # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


mgr = EnrollmentManager()


def recipient(rid: str, state: str, photos: int, embedded: bool) -> None:
    mgr.create_recipient_folder(rid)
    for i in range(photos):
        mgr.save_photo(rid, b"\xff\xd8not-really-a-jpeg" + bytes([i]))
    if embedded:
        mgr.save_embeddings(rid, np.ones((3, 512), dtype=np.float32))
    mgr.set_status(rid, state=state)


recipient("switched_off", "ready", photos=4, embedded=False)   # 2026-09-23
recipient("interrupted", "processing", photos=4, embedded=False)
recipient("healthy", "ready", photos=4, embedded=True)
recipient("draft", "review", photos=4, embedded=False)

worker = EnrollmentWorker(mgr, gallery=None)
queued: list[str] = []
worker.enqueue = queued.append          # capture instead of embedding
worker._resume_pending()

print("\nstart-up resumes unfinished work, never a deliberate switch-off")
check("a recipient whose embeddings were removed stays off",
      "switched_off" not in queued)
check("its status still says what it was",
      json.loads((mgr.base_path / "switched_off" / "status.json").read_text())
      ["state"] == "ready")
check("an enrollment interrupted mid-way is resumed", "interrupted" in queued)
check("a healthy enrollment is left alone", "healthy" not in queued)
check("an uncommitted draft is not auto-enrolled", "draft" not in queued)

shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All enrollment resume checks passed.")
