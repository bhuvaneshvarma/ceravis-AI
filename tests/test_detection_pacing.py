#!/usr/bin/env python3
"""
Prove detection is paced by ACTIVITY: full rate where a person is, idle rate
where the room is empty — and an empty room wakes the moment someone enters.

2026-09-23 (15W, AI off): detection ran every camera at full rate around the
clock and alone asked for 0.73 s of GPU per second; with pose on top the GPU was
oversubscribed, which is where the latency and the over-current peaks came from.

Pure python, fake clock + fake detector; no TensorRT. Runs on the dev box:

    python tests/test_detection_pacing.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

from config.settings import settings                      # noqa: E402
from detection import detection_runner as dr              # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


NOW = [100.0]
dr.time.monotonic = lambda: NOW[0]
dr.time.perf_counter = lambda: NOW[0]

PEOPLE = {"KITCHEN": True, "BEDROOM": False}
runs = {"KITCHEN": 0, "BEDROOM": 0}


class Frames:
    """Each camera delivers a new frame every 1/15 s (a 15 fps reader)."""
    def get_all_latest(self):
        fid = int(NOW[0] * 15)
        return {c: NS(camera_id=c, frame_id=fid, frame=None, timestamp=None)
                for c in PEOPLE}


class Detector:
    def detect(self, frame, camera_id, frame_id, timestamp):
        runs[camera_id] += 1
        dets = [NS(bbox=NS(x1=0, y1=0, x2=10, y2=20))] if PEOPLE[camera_id] else []
        return NS(camera_id=camera_id, detections=dets)


class Buf:
    def update(self, _r):
        pass


r = dr.DetectionRunner(Frames(), Buf())
r._detector = Detector()
r._drop_excluded = lambda res: res


def run_for(secs: float) -> None:
    end = NOW[0] + secs
    while NOW[0] < end:
        NOW[0] += r._process_all_frames()


settings.detection_fps, settings.detection_idle_fps = 10.0, 2.0
settings.detection_active_hold_secs = 10.0

print("\n1. a busy room runs at full rate, an empty one at the idle rate")
run_for(20.0)
check(f"KITCHEN (person) ~10 fps: {runs['KITCHEN'] / 20:.1f}",
      9.0 <= runs["KITCHEN"] / 20 <= 10.5)
check(f"BEDROOM (empty) ~2 fps: {runs['BEDROOM'] / 20:.1f}",
      1.5 <= runs["BEDROOM"] / 20 <= 2.5)

print("\n2. someone walks into the empty room")
PEOPLE["BEDROOM"] = True
before, t0 = runs["BEDROOM"], NOW[0]
while runs["BEDROOM"] == before:
    NOW[0] += r._process_all_frames()
check(f"seen within one idle interval ({NOW[0] - t0:.2f}s <= 0.5s)",
      NOW[0] - t0 <= 0.55)
runs["BEDROOM"] = 0
run_for(10.0)
check(f"and from then on at full rate: {runs['BEDROOM'] / 10:.1f} fps",
      runs["BEDROOM"] / 10 >= 9.0)

print("\n3. they leave: full rate is held, then the room goes idle")
PEOPLE["BEDROOM"] = False
runs["BEDROOM"] = 0
run_for(5.0)
check(f"held at full rate for the hold time: {runs['BEDROOM'] / 5:.1f} fps",
      runs["BEDROOM"] / 5 >= 9.0)
run_for(10.0)
runs["BEDROOM"] = 0
run_for(10.0)
check(f"then idle again: {runs['BEDROOM'] / 10:.1f} fps",
      1.5 <= runs["BEDROOM"] / 10 <= 2.5)

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All detection-pacing checks passed.")
