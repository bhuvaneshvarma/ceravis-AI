#!/usr/bin/env python3
"""
Sanity check for the media wiring — ONE connection per camera, and recording on
the camera's own native main stream.

This is the load-bearing invariant behind both the stability work and the
recording-quality work, so it is asserted rather than assumed:

  * a camera resolves to exactly ONE MediaMTX source — the live path the AI
    reads and the WebRTC links play. Nothing anywhere asks the camera for a
    second stream (a second pull on a WiFi camera is bandwidth taken straight
    from the first, which is what destabilised the AI while live view held up).
  * recording feeds off that same path, so footage is full native quality.
  * the generated mediamtx.yml matches: one `source:` line per camera, plus the
    loopback AAC republish that is what actually gets recorded.
  * start-up pruning deletes recording folders no camera writes to any more
    (an older release recorded a separate `<cam>-rec-aac` sub-stream, and
    MediaMTX's own retention would never expire those) — but ONLY once they are
    past the retention window, so live footage is never touched.

Pure filesystem + string assertions: no camera, no MediaMTX, no network — runs
on the dev box as well as on the device. Section 6 checks an edge_id change
(care-recipient switch) the same way: the old path dropped, ONE pull left.

    python tests/test_media_paths.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

from config.settings import settings                       # noqa: E402
from livestream import mediamtx_client as mtx              # noqa: E402
from livestream.mediamtx_supervisor import MediaMTXSupervisor  # noqa: E402
from schemas.cameras import Camera                         # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def cam(room: str = "LIVING ROOM") -> Camera:
    return Camera(room_name=room, rtsp_url="rtsp://u:p@192.168.0.250:554/stream1")


# --------------------------------------------------------------------------
print("\n1. one camera -> one stream, and recording rides on it")
c = cam()
mtx.edge_prefix = lambda: ""                    # LAN device: no fleet prefix
check("the record source IS the live path (no second camera pull)",
      mtx.record_source_name(c) == mtx.stream_path(c.camera_id) == "LIVING_ROOM")
# record_rtsp_url survives as a RESERVED key (the app-server saveCamera contract
# carries recordRtspUrl, and every stored record must keep one shape) but it is
# inert: held empty, read by nothing. What matters is that it cannot bring the
# second camera pull back — so assert the behaviour, not the field's absence.
check("the reserved second-stream field is empty",
      getattr(c, "record_rtsp_url", "") == "")
check("and setting it changes NOTHING about what we record",
      mtx.record_source_name(
          cam_with_legacy_url := Camera(room_name="LIVING ROOM",
                                        rtsp_url=c.rtsp_url,
                                        record_rtsp_url="rtsp://old/stream2")
      ) == mtx.stream_path(cam_with_legacy_url.camera_id) == "LIVING_ROOM")

_real_audio = mtx.audio_transcode_active
mtx.audio_transcode_active = lambda: True
check("with AAC audio the recorded path is the slash-free <cam>-aac",
      mtx.record_path_name(c) == "LIVING_ROOM-aac")
mtx.audio_transcode_active = lambda: False
check("without ffmpeg the live path is recorded directly",
      mtx.record_path_name(c) == "LIVING_ROOM")
mtx.audio_transcode_active = _real_audio


# --------------------------------------------------------------------------
print("\n2. the same holds on a fleet device (live path carries the edge_id)")
mtx.edge_prefix = lambda: "abc123/"
check("live path is prefixed for frp routing",
      mtx.stream_path("LIVING_ROOM") == "abc123/LIVING_ROOM")
mtx.audio_transcode_active = lambda: True
check("the RECORDED path stays slash-free (flat folders, plain record toggle)",
      mtx.record_path_name(c) == "LIVING_ROOM-aac")
check("but it is fed from the prefixed live path",
      mtx.record_source_name(c) == "abc123/LIVING_ROOM")
mtx.audio_transcode_active = _real_audio
mtx.edge_prefix = lambda: ""


# --------------------------------------------------------------------------
print("\n3. the generated mediamtx.yml pulls each camera exactly once")
cams = [cam("LIVING ROOM"), cam("KITCHEN")]
import livestream.mediamtx_supervisor as sup                # noqa: E402
sup.CameraConfig = lambda: type("_C", (), {"get_all": staticmethod(lambda: cams)})()
mtx.audio_transcode_active = lambda: True
sup.audio_transcode_active = lambda: True

tmp = Path(tempfile.mkdtemp(prefix="ceravis-mtx-"))
supervisor = MediaMTXSupervisor()
supervisor._config_file = tmp / "mediamtx.yml"
supervisor._write_config()
yml = supervisor._config_file.read_text(encoding="utf-8")

check("one `source:` line per camera - no second profile is dialled",
      yml.count("    source: ") == len(cams))
check("no -rec path is emitted", "-rec" not in yml)
check("each camera's AAC republish path is present",
      yml.count("    runOnInit: ") == len(cams))
check("the republish pulls LOOPBACK, never the camera again",
      yml.count("-i rtsp://127.0.0.1:") == len(cams))
check("the republish copies the video (native quality, no re-encode)",
      yml.count("-c:v copy") == len(cams))
check("no speculative reader-queue tuning is emitted",
      "writeQueueSize" not in yml)
shutil.rmtree(tmp, ignore_errors=True)
mtx.audio_transcode_active = _real_audio


# --------------------------------------------------------------------------
print("\n4. start-up pruning removes stranded footage, never live footage")
from recording.controller import RecordingController        # noqa: E402
import recording.controller as rc                           # noqa: E402

root = Path(tempfile.mkdtemp(prefix="ceravis-rec-"))
rc._record_root = lambda: root
old = time.time() - (settings.record_retention_hours + 2) * 3600

for name, mtime in (("LIVING_ROOM-aac", time.time()),      # current, in use
                    ("LIVING_ROOM-rec-aac", old),          # previous release
                    ("KITCHEN-rec-aac", time.time()),      # orphan, still fresh
                    ("GONE-aac", old)):                    # deleted camera
    folder = root / name
    folder.mkdir()
    seg = folder / "2026-08-14_10-00-00-000000.ts"
    seg.write_bytes(b"x")
    os.utime(seg, (mtime, mtime))

ctrl = RecordingController.__new__(RecordingController)
ctrl._cameras = type("_C", (), {"get_all": staticmethod(lambda: [cams[0]])})()
mtx.audio_transcode_active = lambda: True
ctrl._prune_orphans()
mtx.audio_transcode_active = _real_audio

check("the folder the camera writes to is kept",
      (root / "LIVING_ROOM-aac").is_dir())
check("the previous release's sub-stream folder is reclaimed",
      not (root / "LIVING_ROOM-rec-aac").exists())
check("a removed camera's aged-out footage is reclaimed",
      not (root / "GONE-aac").exists())
check("an orphan still INSIDE the retention window is left alone",
      (root / "KITCHEN-rec-aac").is_dir())
shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
print("\n5. pruning never eats a NESTED live tree (the no-ffmpeg fleet path)")
# Without ffmpeg the live path itself is recorded, and on a fleet device that
# name carries a slash — so footage lands in <root>/<edge_id>/<cam>/ and the
# top-level entry is a PARENT of live footage, not an orphan.
root = Path(tempfile.mkdtemp(prefix="ceravis-rec2-"))
rc._record_root = lambda: root
mtx.edge_prefix = lambda: "abc123/"
mtx.audio_transcode_active = lambda: False          # no ffmpeg on this device

nested = root / "abc123" / "LIVING_ROOM"
nested.mkdir(parents=True)
seg = nested / "2026-08-14_10-00-00-000000.ts"
seg.write_bytes(b"x")
stale_tree = root / "oldedge999" / "LIVING_ROOM"    # edge_id changed: whole tree stale
stale_tree.mkdir(parents=True)
old_seg = stale_tree / "2026-08-14_10-00-00-000000.ts"
old_seg.write_bytes(b"x")
os.utime(old_seg, (old, old))

ctrl._prune_orphans()
check("the parent of a live nested folder is NOT deleted", nested.is_dir())
check("its footage survives", seg.exists())
check("a stale tree from a previous edge_id is still reclaimed",
      not (root / "oldedge999").exists())

shutil.rmtree(root, ignore_errors=True)
mtx.edge_prefix = lambda: ""
mtx.audio_transcode_active = _real_audio


# --------------------------------------------------------------------------
print("\n6. an edge_id change keeps ONE connection per camera and moves the AI reader")
from types import SimpleNamespace                          # noqa: E402
from api import account_routes as acct                     # noqa: E402

dropped: list[str] = []
_real_drop = mtx._drop_quietly
mtx._drop_quietly = dropped.append
mtx.drop_previous_live_path("LOUNGE", "oldedge999")
mtx.drop_previous_live_path("LOUNGE", "")
mtx._drop_quietly = _real_drop
check("the PREVIOUS token's live path is the one dropped ('<old>/<cam>', or '<cam>')",
      dropped == ["oldedge999/LOUNGE", "LOUNGE"])

lounge = Camera(room_name="LOUNGE", rtsp_url="rtsp://u:p@10.42.0.250:554/stream1")
living = Camera(room_name="LIVING ROOM", rtsp_url="rtsp://u:p@10.42.0.251:554/stream2",
                ai_rtsp_url="rtsp://u:p@10.42.0.251:554/stream1")
parked = Camera(room_name="KITCHEN", rtsp_url="rtsp://u:p@10.42.0.252:554/stream1")
events: list[tuple[str, str]] = []


class _Cameras:                                            # the stored cameras.json
    def get_all(self):
        return [lounge, living, parked]

    def update(self, camera_id, _camera):
        events.append(("store", camera_id))


class _Readers:                                            # the running CameraManager
    def get_status(self):
        return {c.camera_id: SimpleNamespace(is_running=c is not parked)
                for c in (lounge, living, parked)}

    def restart_camera(self, camera_id):
        events.append(("reader", camera_id))


_saved = {n: getattr(acct, n) for n in ("CameraConfig", "media_backbone_up",
                                        "drop_previous_live_path", "sync_camera", "webrtc_url")}
acct.CameraConfig = _Cameras
acct.media_backbone_up = lambda: True
acct.drop_previous_live_path = lambda cid, old: events.append(("drop", f"{old}/{cid}"))
acct.sync_camera = lambda c: events.append(("sync", c.camera_id))
acct.webrtc_url = lambda cid, base: f"{base}/newedge/{cid}"
acct._repoint_live_paths("https://edge.test", "oldedge999", _Readers())
check("the old path goes BEFORE the new one is pulled, for every camera",
      [e for e in events if e[0] in ("drop", "sync")] == [
          ("drop", "oldedge999/LOUNGE"), ("sync", "LOUNGE"),
          ("drop", "oldedge999/LIVING_ROOM"), ("sync", "LIVING_ROOM"),
          ("drop", "oldedge999/KITCHEN"), ("sync", "KITCHEN")])
check("every stored link is refreshed",
      [cid for k, cid in events if k == "store"] == ["LOUNGE", "LIVING_ROOM", "KITCHEN"])
check("only the RUNNING reader on the live path restarts (not '<cam>-ai', not a stopped one)",
      [cid for k, cid in events if k == "reader"] == ["LOUNGE"])
check("each restart comes after that camera's new path exists",
      events.index(("reader", "LOUNGE")) > events.index(("sync", "LOUNGE")))
events.clear()
acct._repoint_live_paths("https://edge.test", "oldedge999", None)
check("with no pipeline up, paths and links still move and nothing breaks",
      ("sync", "LOUNGE") in events and not any(k == "reader" for k, _ in events))
for n, v in _saved.items():
    setattr(acct, n, v)


# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All media-path checks passed.")
