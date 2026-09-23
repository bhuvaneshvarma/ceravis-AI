#!/usr/bin/env python3
"""
Prove a LOST enrollment gallery rebuilds itself at start-up.

2026-09-23: the recipient's body/ folder (the ReID embeddings) vanished between
two restarts while the photos it was built from were still on disk. The device
came up with an empty gallery, so tracking, ReID, pose, falls and no-motion were
all off for 20 minutes until someone noticed and re-enrolled by hand.

Covered:
  a 'ready' recipient with photos but no embeddings is re-queued (rebuilt);
  a 'ready' recipient WITH embeddings is left alone;
  a recipient still in review (media added, never committed) is left alone;
  a 'ready' recipient with nothing to rebuild from is left alone.

Pure python; no TensorRT. Runs on the dev box:

    python tests/test_enroll_selfheal.py
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


recipient("lost", "ready", photos=4, embedded=False)      # the 2026-09-23 case
recipient("healthy", "ready", photos=4, embedded=True)
recipient("draft", "review", photos=4, embedded=False)
recipient("empty", "ready", photos=0, embedded=False)

worker = EnrollmentWorker(mgr, gallery=None)
queued: list[str] = []
worker.enqueue = queued.append          # capture instead of embedding
worker._resume_pending()

print("\na lost gallery is rebuilt from the stored photos")
check("the recipient whose embeddings vanished is re-queued", "lost" in queued)
check("a healthy enrollment is left alone", "healthy" not in queued)
check("an uncommitted draft is not auto-enrolled", "draft" not in queued)
check("nothing to rebuild from -> nothing queued", "empty" not in queued)
check("status of the healthy one untouched",
      json.loads((mgr.base_path / "healthy" / "status.json").read_text())["state"]
      == "ready")

shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All enrollment self-heal checks passed.")
