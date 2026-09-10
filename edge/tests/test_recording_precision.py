"""
Recording-trigger precision — the layers that stop night IR phantoms (a light
high-back chair read as a seated person) flooding the archive with false clips.

Each gate is tested in BOTH directions: it must reject the phantom AND still
pass a real person, because a recorder that never records looks the same as one
that works until the night someone actually falls.

Run:  PYTHONPATH=edge python edge/tests/test_recording_precision.py
"""
import sys

from common import clock
from config.settings import settings
from common.zone_resolver import ZoneResolver
from detection.detection_schema import (BoundingBox, Detection, DetectionClass,
                                         DetectionResult)
from recording.controller import RecordingController


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
    """Stands in for ZoneConfig — returns fixed zones, no file."""
    def __init__(self, zones): self._z = zones
    def get_for_camera(self, cam): return [z for z in self._z if z["camera_id"] == cam]


def _controller(zones=()):
    c = RecordingController.__new__(RecordingController)   # skip MediaMTX/config init
    c._qual_polls = {}
    c._zones = ZoneResolver(_FakeZones(list(zones)))
    return c


print("\n1. size gate: a real-sized person records, a tiny 'person' does not")
c = _controller()
big = _result([_det(500, 200, 700, 560)])            # 200x360 = 10% of frame
check("a room-sized person qualifies", c._qualifying_person("camA", big))
tiny = _result([_det(500, 200, 540, 240)])           # 40x40 = 0.17% — a TV/photo person
check("a tiny distant/on-screen person is rejected",
      not c._qualifying_person("camA", tiny))
fallen = _result([_det(300, 400, 700, 520)])         # 400x120 wide+short = 6.5%
check("a FALLEN (wide, short) person still qualifies — area, not height",
      c._qualifying_person("camA", fallen))


print("\n2. ignore zone: a person whose FOOT lands in a drawn TV region is skipped")
tv = {"camera_id": "camA", "zone_name": "TV wall",
      "polygon": [[600, 100], [900, 100], [900, 400], [600, 400]]}
cz = _controller([tv])
on_screen = _result([_det(700, 150, 800, 380)])      # foot (750,380) INSIDE the TV zone
check("a person on the TV is not recorded", not cz._qualifying_person("camA", on_screen))
in_front = _result([_det(700, 300, 820, 620)])       # foot (760,620) on the floor BELOW
check("a real person standing in FRONT of the TV still records",
      cz._qualifying_person("camA", in_front))


print("\n3. unknown frame size -> size gate is skipped, never withhold footage")
c2 = _controller()
no_dims = _result([_det(500, 200, 540, 240)], fw=0, fh=0)
check("with no frame size a small box is still allowed (fail-open)",
      c2._qualifying_person("camA", no_dims))


print("\n4. persistence: a flicker never opens a clip; a steady person does")
orig = (settings.record_start_confirm_polls, settings.record_start_window_polls)
settings.record_start_confirm_polls, settings.record_start_window_polls = 3, 4
try:
    c3 = _controller()
    r1 = c3._present("camA", True)
    r2 = c3._present("camA", True)
    check("two qualifying polls are not yet 'present'", not r1 and not r2)
    check("the third consecutive qualifying poll is", c3._present("camA", True))

    c4 = _controller()
    got = [c4._present("camA", q) for q in (True, False, False, False)]
    check("a one-poll flicker never becomes 'present'", not any(got), str(got))

    c5 = _controller()
    seq = [c5._present("camA", q) for q in (True, False, True, True)]
    check("3-of-4 (intermittent but real) does become present", seq[-1] is True,
          str(seq))
finally:
    settings.record_start_confirm_polls, settings.record_start_window_polls = orig


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll recording-precision checks passed.")
