#!/usr/bin/env python3
"""
The media backbone's LIFECYCLE — how MediaMTX is stopped, how its death is
reported, and that a kill mid-write cannot corrupt the device's config.

Written against the real incident (2026-09-23, 03:34, nightly-reboot window):
    mediamtx exited (code -15) … Error writing trailer of …/LOUNGE-aac: Broken pipe
systemd's default KillMode SIGTERMed MediaMTX at the same instant as the app;
MediaMTX v1.9.x handles ONLY SIGINT, so SIGTERM killed it outright, the open
recording was cut, and a "DOWN" alarm fired for what was a routine shutdown.

  1. stop() uses SIGINT (MediaMTX's graceful path), hard-kills only on timeout
  2. an exit is described truthfully (signal name vs exit code)
  3. an outage is ONE report + ONE recovery — a crash-loop never floods
  4. a real crash-looping child through the real supervisor loop
  5. the systemd unit lets the APP order the shutdown (KillMode=mixed)
  6. config writes are atomic — a failed write leaves the old file intact
  7. the AI reader waits until MediaMTX RECEIVES its path, opens with the
     negotiated codec, and never hangs forever in an open. (The 10:50 symptom:
     both readers stuck in open() from boot, so the AI never got a frame —
     nothing detected, nothing recorded, and the monitor showed NO SIGNAL.)

No MediaMTX, no camera, no network — runs on the dev box and on the device.

    python tests/test_backbone_lifecycle.py
"""
from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from livestream import mediamtx_supervisor as sup     # noqa: E402
from configuration.config_store import ConfigStore    # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


class FakeProc:
    """Popen stand-in that records how it was asked to stop."""
    def __init__(self, obeys: bool = True):
        self.obeys, self.calls, self._code = obeys, [], None

    def poll(self):
        return self._code

    def send_signal(self, sig):
        self.calls.append(("signal", sig))
        if self.obeys:
            self._code = 0

    def terminate(self):
        self.calls.append(("terminate",))
        if self.obeys:
            self._code = 0

    def kill(self):
        self.calls.append(("kill",))
        self._code = -9

    def wait(self, timeout=None):
        if self._code is None:
            raise subprocess.TimeoutExpired("mediamtx", timeout)
        return self._code


RECORDS: list[dict] = []
sup.call_log.record = lambda endpoint, ok, **kw: RECORDS.append({"ok": ok, **kw})


# --------------------------------------------------------------------------
print("\n1. stop() asks MediaMTX to close gracefully")
real_os_name = sup.os.name
try:
    sup.os.name = "posix"
    p = FakeProc()
    sup._shutdown(p)
    check("POSIX: SIGINT, the only signal MediaMTX handles",
          p.calls == [("signal", signal.SIGINT)], str(p.calls))
    p = FakeProc(obeys=False)
    sup._shutdown(p, timeout=0.01)
    check("a child that won't go is hard-killed after the timeout",
          p.calls[-1] == ("kill",), str(p.calls))
    sup.os.name = "nt"
    p = FakeProc()
    sup._shutdown(p)
    check("Windows dev box: terminate() (no SIGINT for a child there)",
          p.calls == [("terminate",)], str(p.calls))
finally:
    sup.os.name = real_os_name
p = FakeProc()
p._code = 1
sup._shutdown(p)
check("an already-dead child is left alone", p.calls == [])
sup._shutdown(None)
check("no child at all is a no-op", True)


# --------------------------------------------------------------------------
print("\n2. the exit is described truthfully")
cause = sup._exit_cause(-15)
check("-15 reads as SIGTERM from outside", "SIGTERM" in cause and "outside" in cause, cause)
check("a positive code reads as an exit code", sup._exit_cause(1) == "exited (code 1)")
check("an unknown signal still reads cleanly", "signal 99" in sup._exit_cause(-99))


# --------------------------------------------------------------------------
print("\n3. an outage is one DOWN and one RECOVERED")
s = sup.MediaMTXSupervisor()
RECORDS.clear()
for _ in range(5):                         # a crash-loop: five deaths
    s._outage("exited (code 1)")
check("five deaths in one outage -> ONE DOWN report",
      len(RECORDS) == 1 and RECORDS[0]["ok"] is False, str(RECORDS))
check("DOWN carries the cause", "exited (code 1)" in RECORDS[0].get("error", ""))
s._recovered()
check("recovery closes it with ONE ok INFO record",
      len(RECORDS) == 2 and RECORDS[1]["ok"] is True
      and RECORDS[1]["label"].startswith("INFO · Media backbone RECOVERED"), str(RECORDS[-1:]))
s._outage("was killed by SIGTERM (from outside CERAVIS)")
check("a NEW death after recovery is a new outage -> reported again", len(RECORDS) == 3)


# --------------------------------------------------------------------------
print("\n4. the real supervisor loop against a real crash-looping child")
tmp = Path(tempfile.mkdtemp())
crash = tmp / "crash.py"
crash.write_text("import sys; sys.exit(3)\n")
s = sup.MediaMTXSupervisor()
s._config_file = crash                     # the 'binary' is python, the 'config' the script
s._log_file = tmp / "mediamtx.log"
s._write_config = lambda: None
real_bin = sup.settings.mediamtx_binary
RECORDS.clear()
try:
    object.__setattr__(sup.settings, "mediamtx_binary", sys.executable)
    s._running = True
    import threading
    t = threading.Thread(target=s._run, daemon=True)
    t.start()
    time.sleep(4.5)                        # ~3 deaths at 1s/2s backoff
    s.stop()
    t.join(timeout=35)
finally:
    object.__setattr__(sup.settings, "mediamtx_binary", real_bin)
downs = [r for r in RECORDS if r["ok"] is False]
check("repeated deaths -> exactly ONE DOWN", len(downs) == 1, str(RECORDS))
check("…naming the real exit code", downs and "code 3" in downs[0].get("error", ""),
      str(downs))
check("stop() ends the loop (no respawn after our own stop)", not t.is_alive())


# --------------------------------------------------------------------------
print("\n5. systemd lets the app order its own shutdown")
unit = io.open(ROOT / "edge/infra/systemd/ceravis.service", encoding="utf-8").read()
keys = [l.strip() for l in unit.splitlines() if l.strip() and not l.lstrip().startswith("#")]
check("KillMode=mixed (SIGTERM to the app only, not MediaMTX)", "KillMode=mixed" in keys)
check("graceful HTTP shutdown is bounded",
      any(k.startswith("Environment=UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=") for k in keys))
check("a stop timeout is declared", any(k.startswith("TimeoutStopSec=") for k in keys))


# --------------------------------------------------------------------------
print("\n6. config writes are atomic")
store = ConfigStore()
store.base_path = tmp
store.save("cameras.json", [{"room_name": "LOUNGE"}])
check("save -> load round-trips", store.load("cameras.json") == [{"room_name": "LOUNGE"}])
try:
    store.save("cameras.json", [{"bad": object()}])     # dies mid-write
except TypeError:
    pass
check("a write that dies midway leaves the OLD file intact",
      store.load("cameras.json") == [{"room_name": "LOUNGE"}])


# --------------------------------------------------------------------------
print("\n7. the AI reader never waits forever")
import threading                                          # noqa: E402
import numpy as np                                        # noqa: E402
from livestream import mediamtx_client as mtx             # noqa: E402
from ingestion import rtsp_reader as rr                   # noqa: E402
from ingestion.frame_buffer import FrameBuffer            # noqa: E402
from schemas.cameras import Camera                        # noqa: E402

real_get = mtx._get_path
try:
    mtx._get_path = lambda name: None
    check("MediaMTX down -> not ready", mtx.source_state("x") == (False, None))
    mtx._get_path = lambda name: {"ready": False, "tracks": ["H264"]}
    check("path not receiving -> not ready", mtx.source_state("x") == (False, None))
    mtx._get_path = lambda name: {"ready": True, "tracks": ["H265", "MPEG-4 Audio"]}
    check("receiving -> ready + the NEGOTIATED codec",
          mtx.source_state("x") == (True, "h265"), str(mtx.source_state("x")))
finally:
    mtx._get_path = real_get

cam = Camera(room_name="LOUNGE", rtsp_url="rtsp://u:p@10.42.0.250:554/stream1")
reader = rr.RTSPReader(cam, FrameBuffer(),
                       source_url="rtsp://127.0.0.1:8554/edgeXYZ/LOUNGE")
check("reader knows its OWN MediaMTX path (slash kept)",
      reader._mtx_path == "edgeXYZ/LOUNGE", str(reader._mtx_path))

OPENS: list = []
CAPS: list = []


class FakeCap:
    def __init__(self, src, opened=True, block: threading.Event | None = None):
        OPENS.append(src)
        if block is not None:
            block.wait()
        self.src, self.opened, self.released = src, opened, False
        CAPS.append(self)

    def isOpened(self):
        return self.opened

    def set(self, *a):
        return True

    def get(self, prop):
        return FakeCap.FPS

    FPS = 0.0

    def read(self):
        return True, np.zeros((36, 64, 3), np.uint8)

    def release(self):
        self.released = True


real_vc, real_timeout, real_poll = rr.cv2.VideoCapture, rr._OPEN_TIMEOUT_SECS, rr._READY_POLL_SECS
real_prod = rr.settings.is_production
real_state = mtx.source_state
try:
    rr._OPEN_TIMEOUT_SECS = 0.3
    gate = threading.Event()
    rr.cv2.VideoCapture = lambda src, *a: FakeCap(src, block=gate)
    t0 = time.monotonic()
    got = reader._open("hw-gst-h264", "rtspsrc ! fake")
    check("a hung open is ABANDONED at the timeout, not waited on forever",
          got is None and time.monotonic() - t0 < 2.0)
    check("…and remembered as still stuck",
          reader._hung_open is not None and reader._hung_open.is_alive())

    object.__setattr__(rr.settings, "is_production", True)
    OPENS.clear()
    rr.cv2.VideoCapture = lambda src, *a: FakeCap(src)
    reader._connect("h264")
    check("while a GStreamer open is stuck, only FFmpeg is tried (no pile-up)",
          OPENS == [reader._source_url], str(OPENS))
    gate.set()
    reader._hung_open.join(2)
    check("the abandoned open, when it finally returns, releases itself",
          any(c.src == "rtspsrc ! fake" and c.released for c in CAPS))

    OPENS.clear()
    reader._capture = None
    rr.cv2.VideoCapture = lambda src, *a: FakeCap(src, opened=False)
    reader._connect("h265")
    gst = [o for o in OPENS if o != reader._source_url]
    check("known codec -> only that codec's pipelines are tried",
          bool(gst) and all("rtph265depay" in o for o in gst), str(gst))

    # A camera sending far more than the AI takes: the hardware decoder is told
    # to hand over only every Nth frame (learned once, then reused).
    for fps, want in ((25.0, 2), (0.0, 1)):
        skip = rr.RTSPReader(cam, FrameBuffer(),
                             source_url="rtsp://127.0.0.1:8554/edgeXYZ/LOUNGE")
        FakeCap.FPS = fps
        OPENS.clear(); CAPS.clear()
        rr.cv2.VideoCapture = lambda src, *a: FakeCap(src)
        object.__setattr__(rr.settings, "is_production", True)
        skip._connect("h265")
        gst = [o for o in OPENS if o != skip._source_url]
        if want > 1:
            check(f"a {fps:.0f} fps camera is reopened decoding every {want}nd frame",
                  len(gst) == 2 and "drop-frame-interval" not in gst[0]
                  and f"drop-frame-interval={want}" in gst[1] and CAPS[0].released, str(gst))
        else:
            check("an unreported camera rate skips nothing (one open)",
                  len(gst) == 1 and "drop-frame-interval" not in gst[0], str(gst))
        OPENS.clear()
        skip._capture = None
        skip._connect("h265")
        check(f"…the learned skip is reused on reconnect (fps={fps:.0f})",
              len(OPENS) == 1 and (f"drop-frame-interval={want}" in OPENS[0]) == (want > 1),
              str(OPENS))
    FakeCap.FPS = 0.0

    # The whole reader, as at boot: the path is not received yet -> it waits
    # without opening anything -> MediaMTX receives it -> it opens -> frames.
    object.__setattr__(rr.settings, "is_production", False)
    rr._READY_POLL_SECS = 0.05
    states = iter([(False, None)] * 3)
    mtx.source_state = lambda name: next(states, (True, "h264"))
    OPENS.clear()
    rr.cv2.VideoCapture = lambda src, *a: FakeCap(src)
    fb = FrameBuffer()
    boot = rr.RTSPReader(cam, fb, source_url="rtsp://127.0.0.1:8554/edgeXYZ/LOUNGE",
                         target_fps=50)
    boot.start()
    time.sleep(1.0)
    fed = fb.get(cam.camera_id) is not None      # (stop() clears the buffer)
    boot.stop()
    boot.join(2)
    check("no open is attempted while MediaMTX isn't receiving the path",
          len(OPENS) == 1, str(OPENS))
    check("once it is, the AI gets frames",
          boot.frames_captured > 0 and fed,
          str(boot.frames_captured))
finally:
    rr.cv2.VideoCapture, rr._OPEN_TIMEOUT_SECS = real_vc, real_timeout
    rr._READY_POLL_SECS = real_poll
    mtx.source_state = real_state
    object.__setattr__(rr.settings, "is_production", real_prod)


if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
    sys.exit(1)
print("\nall backbone-lifecycle checks passed")
