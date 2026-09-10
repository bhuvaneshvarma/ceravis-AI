"""
Recording-trigger precision + ignore-zone masking + proof frames — the layers
that stop night IR phantoms (a light high-back chair read as a seated person, or
a person on a TV) flooding the archive and the visitor stream with false events.

Each gate is tested in BOTH directions: it must reject the phantom AND still
pass a real person, because a recorder that never records looks the same as one
that works until the night someone actually falls.

Run:  PYTHONPATH=edge python edge/tests/test_recording_precision.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from common import clock
from config.settings import settings
from detection.detection_runner import DetectionRunner
from common.zone_resolver import ZoneResolver
from detection.detection_schema import (BoundingBox, Detection, DetectionClass,
                                         DetectionResult)
from recording.controller import RecordingController
from recording.proof import ProofWriter


FW, FH = 1280, 720
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def _det(x1, y1, x2, y2, conf=0.9):
    return Detection(camera_id="camA", frame_id=1, timestamp=clock.now(),
                     class_id=0, class_name=DetectionClass.PERSON, confidence=conf,
                     bbox=BoundingBox(x1=float(x1), y1=float(y1),
                                      x2=float(x2), y2=float(y2)))


def _result(dets, fw=FW, fh=FH):
    return DetectionResult(camera_id="camA", frame_id=1, timestamp=clock.now(),
                           detections=dets, frame_w=fw, frame_h=fh)


class _FakeZones:
    def __init__(self, zones): self._z = zones
    def get_for_camera(self, cam): return [z for z in self._z if z["camera_id"] == cam]


def _controller():
    c = RecordingController.__new__(RecordingController)   # skip MediaMTX/config init
    c._qual_polls = {}
    return c


print("\n1. size gate (recording): a real-sized person records, a tiny one does not")
c = _controller()
big = _result([_det(500, 200, 700, 560)])            # 200x360 = 10% of frame
check("a room-sized person qualifies", c._qualifying_person("camA", big))
tiny = _result([_det(500, 200, 540, 240)])           # 40x40 = 0.17% — a TV/photo person
check("a tiny distant/on-screen person is rejected",
      not c._qualifying_person("camA", tiny))
fallen = _result([_det(300, 400, 700, 520)])         # 400x120 wide+short = 6.5%
check("a FALLEN (wide, short) person still qualifies — area, not height",
      c._qualifying_person("camA", fallen))
no_dims = _result([_det(500, 200, 540, 240)], fw=0, fh=0)
check("unknown frame size -> size gate skipped (fail-open, never withhold)",
      c._qualifying_person("camA", no_dims))


print("\n2. ignore zone (detection source): masks EVERY consumer, not just recording")
tv = {"camera_id": "camA", "zone_name": "TV wall",
      "polygon": [[600, 100], [900, 100], [900, 400], [600, 400]]}
dr = DetectionRunner.__new__(DetectionRunner)
dr._zones = ZoneResolver(_FakeZones([tv]))
res = _result([_det(700, 150, 800, 380),             # ON the TV (foot 750,380 inside)
               _det(700, 300, 820, 620)])            # IN FRONT (foot 760,620 below)
out = dr._drop_excluded(res)
check("the person on the TV is dropped at the source (no track/visitor/clip)",
      len(out.detections) == 1)
check("a real person standing in FRONT of the TV survives",
      out.detections and out.detections[0].bbox.y2 == 620.0)
none_zone = DetectionRunner.__new__(DetectionRunner)
none_zone._zones = ZoneResolver(_FakeZones([]))
check("no ignore zone drawn -> nothing is dropped (opt-in)",
      len(none_zone._drop_excluded(_result([_det(700, 150, 800, 380)])).detections) == 1)


print("\n3. persistence: a flicker never opens a clip; a steady person does")
orig = (settings.record_start_confirm_polls, settings.record_start_window_polls)
settings.record_start_confirm_polls, settings.record_start_window_polls = 3, 4
try:
    c3 = _controller()
    r1, r2 = c3._present("camA", True), c3._present("camA", True)
    check("two qualifying polls are not yet 'present'", not r1 and not r2)
    check("the third consecutive qualifying poll is", c3._present("camA", True))

    c4 = _controller()
    got = [c4._present("camA", q) for q in (True, False, False, False)]
    check("a one-poll flicker never becomes 'present'", not any(got), str(got))

    c5 = _controller()
    seq = [c5._present("camA", q) for q in (True, False, True, True)]
    check("3-of-4 (intermittent but real) does become present", seq[-1] is True, str(seq))
finally:
    settings.record_start_confirm_polls, settings.record_start_window_polls = orig


print("\n4. proof frame: an annotated still is written on start, and expires")


class _FakeFB:
    def __init__(self, frame): self._frame = frame
    def get(self, cam):
        class _FD:  # minimal FrameData stand-in
            pass
        fd = _FD(); fd.frame = self._frame
        return fd


import recording.proof as proofmod                                # noqa: E402
tmp = Path(tempfile.mkdtemp(prefix="ceravis_proof_"))
o_root, o_enabled, o_ret = (proofmod._proof_root, settings.record_proof_frames,
                            settings.record_retention_hours)
try:
    proofmod._proof_root = lambda: tmp / "recordings_proof"       # redirect off the real data dir
    settings.record_proof_frames = True
    frame = np.zeros((FH, FW, 3), dtype=np.uint8)
    pw = ProofWriter(_FakeFB(frame))
    pw.capture("camA", _result([_det(500, 200, 700, 560)]))
    jpgs = list((tmp / "recordings_proof").rglob("*.jpg"))
    check("a proof still is written when a clip opens", len(jpgs) == 1, str(jpgs))

    settings.record_proof_frames = False
    pw2 = ProofWriter(_FakeFB(frame))
    pw2.capture("camA", _result([_det(500, 200, 700, 560)]))
    check("disabled -> no still written",
          len(list((tmp / "recordings_proof").rglob("*.jpg"))) == 1)

    # sweep: age the file past retention and confirm it is pruned
    settings.record_proof_frames = True
    settings.record_retention_hours = 0
    if jpgs:
        old = time.time() - 3600
        os.utime(jpgs[0], (old, old))
    pw.sweep()
    check("a proof still past the retention window is swept",
          not list((tmp / "recordings_proof").rglob("*.jpg")))
finally:
    proofmod._proof_root = o_root
    settings.record_proof_frames, settings.record_retention_hours = o_enabled, o_ret
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll recording-precision checks passed.")
