#!/usr/bin/env python3
"""
Guard against a property being CALLED — the bug that silently killed the AI.

`FaissGallery.size` is a @property. `tracking_runner._reid_ready()` called it as
`self._gallery.size()`, which raises `TypeError: 'int' object is not callable`
on EVERY tracking tick. The runner catches per-tick exceptions and logs them, so
nothing crashed and nothing alarmed — YOLO detection and recording kept working
while tracking, ReID, pose, posture and every rule above them (falls, stillness,
room changes) never ran at all. On screen it looked exactly like "the recipient
is never found". It survived from commit a389965 until the device logs were read.

So this is a STATIC check across the whole tree: for every @property on the
gallery, assert nothing calls it. Static because importing FaissGallery needs
faiss + numpy, which a dev box does not have — and a check that only runs on the
device is a check that does not run.

    python tests/test_gallery_gate.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def properties_of(path: Path, class_name: str) -> set[str]:
    """Names on `class_name` in `path` that are @property (read, never called)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name for item in node.body
                if isinstance(item, ast.FunctionDef)
                and any(isinstance(d, ast.Name) and d.id == "property"
                        for d in item.decorator_list)
            }
    return set()


def called_attributes(path: Path) -> set[tuple[int, str]]:
    """(line, attr) for every `<something>.attr(...)` call in a file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add((node.func.lineno, node.func.attr))
    return out


# --------------------------------------------------------------------------
print("\n1. the gallery's read-only surface is properties, not methods")
gallery_props = properties_of(EDGE / "reid" / "faiss_index.py", "FaissGallery")
check("FaissGallery.size is a @property", "size" in gallery_props)

# --------------------------------------------------------------------------
print("\n2. nothing anywhere CALLS one of them")
# Only files that actually touch a gallery can commit this mistake, and limiting
# the scan to them keeps a same-named method on an unrelated class from tripping.
suspects = [p for p in EDGE.rglob("*.py")
            if "__pycache__" not in str(p)
            and "gallery" in p.read_text(encoding="utf-8").lower()]
check(f"found the files that use a gallery ({len(suspects)})", len(suspects) > 0)

offenders = []
for path in suspects:
    for line, attr in called_attributes(path):
        if attr in gallery_props:
            offenders.append(f"{path.relative_to(ROOT)}:{line} calls .{attr}()")
check(f"no property is invoked {offenders or ''}", not offenders)

# --------------------------------------------------------------------------
print("\n3. the gate itself reads the property")
gate = (EDGE / "tracking" / "tracking_runner.py").read_text(encoding="utf-8")
check("_reid_ready compares the value, not a call",
      "self._gallery.size > 0" in gate)
check("and the old broken form is gone", "_gallery.size()" not in gate)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All gallery-gate checks passed.")
