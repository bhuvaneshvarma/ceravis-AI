#!/usr/bin/env python3
"""
The camera record has ONE fixed shape — fields get added over time, never removed.

Two consumers depend on that and neither can be allowed to drift:

  * the app-server saveCamera call, whose 16 camelCase keys are a contract with
    the backend team
  * cameras.json, where every record must carry the SAME keys — a field that
    exists on one camera and not another is how a stale value hides

This test exists because both drifted. Deleting record_rtsp_url from the schema
dropped the key from newly-saved records while older ones on disk kept it, so
LOUNGE and LIVING ROOM ended up with different shapes in the same file. The
field is now RESERVED: present in every record, always empty, read by nothing,
so the contract holds without a second recording stream coming back.

No FastAPI needed — the saveCamera key set is read from the source, so this runs
on a dev box.

    python tests/test_camera_record_shape.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from schemas.cameras import Camera, FILE_EXCLUDE               # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# The agreed saveCamera payload, exactly as the backend receives it.
CONTRACT = ["device", "model", "supplier", "room", "url", "rtspUrl",
            "recordRtspUrl", "hlsUrl", "onvifXaddr", "onvifUsername",
            "onvifPassword", "onvifProfileToken", "onvifPtzToken",
            "ptzSupported", "isEnabled", "webrtcUrl"]

# The two bench records exactly as they sat on disk when the drift was spotted.
LOUNGE = {
    "room_name": "LOUNGE",
    "rtsp_url": "rtsp://tapo220:x@192.168.0.251:554/stream1",
    "manufacturer": "tp-link", "model": "Tapo C220", "serial": "7461c644",
    "record_rtsp_url": "rtsp://tapo220:x@192.168.0.251:554/stream2",  # stale
    "onvif_xaddr": "http://192.168.0.251:2020/onvif/device_service",
    "onvif_username": "tapo220", "onvif_password": "p",
    "onvif_profile_token": "profile_1", "onvif_ptz_token": None,
    "ptz_supported": True, "is_enabled": True,
    "webrtc_url": "https://edgeai/NrP/LOUNGE", "hls_url": "",
}
LIVING = {**{k: v for k, v in LOUNGE.items() if k != "record_rtsp_url"},
          "room_name": "LIVING ROOM", "model": "Tapo C260",
          "rtsp_url": "rtsp://tapo260:x@192.168.0.250:554/stream1"}

# --------------------------------------------------------------------------
print("\n1. saveCamera: the wire contract is exactly the agreed 16 keys")
fn = next(n for n in ast.walk(ast.parse(
              (EDGE / "api" / "account_routes.py").read_text(encoding="utf-8")))
          if isinstance(n, ast.FunctionDef) and n.name == "_cloud_camera")
emitted = [k.value for k in
           next(n for n in ast.walk(fn) if isinstance(n, ast.Return)).value.keys]
check("every contract key is sent", not set(CONTRACT) - set(emitted))
check("nothing extra is sent", not set(emitted) - set(CONTRACT))
check("no duplicate keys", len(emitted) == len(set(emitted)))
check("recordRtspUrl is still in the payload", "recordRtspUrl" in emitted)

# --------------------------------------------------------------------------
print("\n2. cameras.json: one shape, whatever a record looked like before")
a = Camera(**LOUNGE).model_dump(exclude=FILE_EXCLUDE)
b = Camera(**LIVING).model_dump(exclude=FILE_EXCLUDE)
check("a record WITH the legacy key and one WITHOUT write identical keys",
      list(a) == list(b))
check("no duplicates", len(a) == len(set(a)))
check("the reserved key is present on both", "record_rtsp_url" in a and "record_rtsp_url" in b)

# --------------------------------------------------------------------------
print("\n3. the reserved field is inert — it cannot resurrect a second pull")
check("a stale value on disk is cleared", a["record_rtsp_url"] == "")
check("a value POSTed by a client is refused too",
      Camera(**{**LIVING, "record_rtsp_url": "rtsp://sneaky/stream2"}
             ).record_rtsp_url == "")
check("the real stream is untouched by any of it",
      a["rtsp_url"].endswith("/stream1") and b["rtsp_url"].endswith("/stream1"))

# --------------------------------------------------------------------------
print("\n4. nothing in the pipeline reads it any more")
hits = []
for path in EDGE.rglob("*.py"):
    if path.name in ("cameras.py", "account_routes.py") or "__pycache__" in str(path):
        continue
    if "record_rtsp_url" in path.read_text(encoding="utf-8"):
        hits.append(path.name)
check(f"no consumer outside the schema + cloud payload {hits or ''}", not hits)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All camera record-shape checks passed.")
