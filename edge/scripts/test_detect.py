#!/usr/bin/env python3
"""
Standalone YOLO person-detection tester.

Opens a camera's live RTSP stream, runs the SAME YOLODetector + engine the
service uses, and shows the detections — in a popup window if a display is
attached, otherwise it prints counts and writes an annotated frame to
/tmp/ceravis_detect.jpg so you can view it over SSH.

Usage (on the Jetson):
    python3 scripts/test_detect.py                      # first enabled camera
    python3 scripts/test_detect.py rtsp://user:pass@ip:554/stream
    python3 scripts/test_detect.py <rtsp_or_camera_id> 0.20   # custom conf

Press ESC or q to quit (windowed mode), or Ctrl+C (headless).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = os.path.dirname(HERE)
sys.path.insert(0, EDGE)

from config.settings import settings                 # noqa: E402
from detection.yolo_detector import YOLODetector     # noqa: E402


def _cameras():
    path = os.path.join(EDGE, "data", "cameras.json")
    return json.load(open(path)) if os.path.exists(path) else []


def _resolve(arg):
    cams = _cameras()
    if arg and arg.lower().startswith("rtsp"):
        return arg, "h264"
    if arg:
        for c in cams:
            if c.get("camera_id") == arg:
                return c["rtsp_url"], c.get("codec", "h264")
    for c in cams:
        if c.get("is_enabled", True):
            return c["rtsp_url"], c.get("codec", "h264")
    return None, None


def _open(url, codec):
    depay = ("rtph265depay ! h265parse ! avdec_h265" if codec == "h265"
             else "rtph264depay ! h264parse ! avdec_h264")
    pipe = (f"rtspsrc location={url} latency=100 ! {depay} ! "
            f"videoconvert ! video/x-raw,format=BGR ! appsink drop=1")
    cap = cv2.VideoCapture(pipe, cv2.CAP_GSTREAMER)
    if cap.isOpened():
        return cap
    print("[test] gstreamer pipeline failed — trying ffmpeg/url …")
    return cv2.VideoCapture(url)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    conf = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
    settings.detection_confidence_threshold = conf
    headless = not os.environ.get("DISPLAY")

    url, codec = _resolve(arg)
    if not url:
        print("No camera found. Pass an RTSP URL, or register a camera first.")
        return 1
    print(f"[test] stream : {url}")
    print(f"[test] engine : {settings.detection_model_path}")
    print(f"[test] conf>= : {conf}   mode: {'headless' if headless else 'window'}")

    det = YOLODetector()
    cap = _open(url, codec)
    if not cap or not cap.isOpened():
        print("[test] could not open stream — check the RTSP URL / network")
        return 1

    n, t0, last = 0, time.time(), 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("[test] frame read failed; retrying…")
                time.sleep(0.4)
                continue
            res = det.detect(frame, "test", n, datetime.now(timezone.utc))
            for d in res.detections:
                b = d.bbox
                cv2.rectangle(frame, (int(b.x1), int(b.y1)),
                              (int(b.x2), int(b.y2)), (0, 220, 120), 2)
                cv2.putText(frame, f"person {d.confidence:.2f}",
                            (int(b.x1), max(0, int(b.y1) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 120), 2)
            cv2.putText(frame, f"persons: {len(res.detections)}", (12, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 255), 2)
            n += 1
            if time.time() - last > 1.0:
                fps = n / max(time.time() - t0, 1e-3)
                print(f"[test] frame {n:5d}  persons={len(res.detections)}  ~{fps:.1f} fps")
                last = time.time()
                if headless:
                    cv2.imwrite("/tmp/ceravis_detect.jpg", frame)

            if not headless:
                cv2.imshow("CERAVIS - detection test (ESC/q to quit)", frame)
                if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if not headless:
            cv2.destroyAllWindows()
    print(f"[test] done — {n} frames processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
