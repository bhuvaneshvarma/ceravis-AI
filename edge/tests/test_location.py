"""
Local test for the LocationRule (no TRT needed) — one tracker, two depths:
room (camera→camera) and area (zone inside the camera).

Run:  PYTHONPATH=edge python edge/tests/test_location.py
"""
import sys
from itertools import count
from types import SimpleNamespace

# the details carry a "→"; don't die on cp1252 Windows consoles
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from detection.detection_schema import BoundingBox
from pose.pose_buffer import PoseBuffer
from pose.posture_buffer import PostureBuffer
from pose.posture_classifier import PostureTracker
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from rules.location_rule import LocationRule
from rules.rule_context import RuleContext
from tracking.track_buffer import TrackBuffer
from tracking.track_schema import Track, TrackResult
from common import clock                       # noqa: E402


_FID = count(1)

ROOMS = {"camA": "Kitchen", "camB": "Living room", "camC": "Kitchen"}


class _Cams:
    def get_by_id(self, cid):
        return SimpleNamespace(room_name=ROOMS.get(cid, ""))


class _Zones:
    """Zone stub: area_for returns whatever the test sets as `current`."""
    def __init__(self) -> None:
        self.current: str | None = None

    def area_for(self, cam, x, y):
        return self.current


def _world():
    tracks, idents = TrackBuffer(), IdentityBuffer()
    ctx = RuleContext(frames=None, detections=None, tracks=tracks,
                      poses=PoseBuffer(), postures=PostureBuffer(),
                      posture_tracker=PostureTracker(), identities=idents)
    return ctx, tracks, idents


def _tick(bufs, cam: str, tid: int, conf: float = 0.9) -> None:
    ctx, tracks, idents = bufs
    now = clock.now()
    fid = next(_FID)
    tracks.update(TrackResult(
        camera_id=cam, frame_id=fid, timestamp=now,
        tracks=[Track(track_id=tid, camera_id=cam, frame_id=fid, timestamp=now,
                      bbox=BoundingBox(x1=150, y1=80, x2=260, y2=480),
                      confidence=0.9)]))
    idents.update(Identity(track_id=tid, camera_id=cam, frame_id=fid,
                           timestamp=now, recipient_id="ceravis-8",
                           is_target=True, confidence=conf))


def test_room_transition_fires_once():
    rule = LocationRule(camera_config=_Cams(), zone_resolver=_Zones())
    bufs = _world()
    events = []
    for _ in range(5):                       # settle in the Kitchen (camA)
        _tick(bufs, "camA", 1)
        events += rule.evaluate(bufs[0])
    for _ in range(5):                       # walk to the Living room (camB)
        _tick(bufs, "camB", 7, conf=0.95)    # re-lock wins on confidence
        events += rule.evaluate(bufs[0])
    kinds = [(e.event_type, e.detail) for e in events]
    print(f"[room move] events={kinds}")
    assert kinds == [("room_transition", "Kitchen → Living room")], \
        "exactly one debounced room_transition with the arrow detail"
    print("[room move] PASS")


def test_same_room_other_camera_is_silent():
    rule = LocationRule(camera_config=_Cams(), zone_resolver=_Zones())
    bufs = _world()
    events = []
    for _ in range(5):
        _tick(bufs, "camA", 1)               # Kitchen
        events += rule.evaluate(bufs[0])
    for _ in range(5):
        _tick(bufs, "camC", 9, conf=0.95)    # ALSO Kitchen
        events += rule.evaluate(bufs[0])
    print(f"[same room] events={[e.event_type for e in events]}")
    assert not events, "two cameras in the same room must not raise a move"
    print("[same room] PASS")


def test_area_transition_within_camera():
    zones = _Zones()
    rule = LocationRule(camera_config=_Cams(), zone_resolver=zones)
    bufs = _world()
    events = []
    zones.current = "stove"
    for _ in range(4):                       # baseline area — no event
        _tick(bufs, "camA", 1)
        events += rule.evaluate(bufs[0])
    zones.current = "dining"
    for _ in range(4):                       # settled area change — one event
        _tick(bufs, "camA", 1)
        events += rule.evaluate(bufs[0])
    kinds = [(e.event_type, e.detail) for e in events]
    print(f"[area move] events={kinds}")
    assert kinds == [("area_transition", "stove → dining")], \
        "exactly one debounced area_transition with the arrow detail"
    print("[area move] PASS")


if __name__ == "__main__":
    test_room_transition_fires_once()
    test_same_room_other_camera_is_silent()
    test_area_transition_within_camera()
    print("ALL LOCATION TESTS PASSED")
