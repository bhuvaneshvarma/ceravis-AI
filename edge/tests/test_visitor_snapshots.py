"""
Visitor motion snapshots — the rebuild of the feature removed in 2afcc77.

Two things had to be true for this to work, and only one of them is the rule:

  1. the rule must fire on MOTION (v1 fired on a clock, so a sleeping visitor
     produced the same burst as a walking one), and
  2. the event must SURVIVE the cloud recipient gate, which drops anything
     without a recipient_id — and a visitor has none BY DEFINITION.

Miss (2) and the rule works perfectly while nothing ever reaches the app server,
which is indistinguishable from the rule being broken.

Run:  PYTHONPATH=edge python edge/tests/test_visitor_snapshots.py
"""
import io
import pathlib
import sys
import time

from common import clock
from config.settings import settings

settings.visitor_snapshot_interval_secs = 0.05      # test scale

from detection.detection_schema import BoundingBox        # noqa: E402
from reid.identity_buffer import IdentityBuffer           # noqa: E402
from reid.identity_schema import Identity                 # noqa: E402
from rules.rule_context import RuleContext                # noqa: E402
from rules.visitor_rule import VisitorRule                # noqa: E402
from tracking.track_buffer import TrackBuffer             # noqa: E402
from tracking.track_schema import Track, TrackResult      # noqa: E402


failures: list[str] = []
_FID = [0]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def _world():
    tracks, idents = TrackBuffer(), IdentityBuffer()
    ctx = RuleContext(frames=None, detections=None, tracks=tracks, poses=None,
                      postures=None, posture_tracker=None, identities=idents)
    return ctx, tracks, idents


def _tick(bufs, people, targets=()):
    """people: {track_id: x_offset}. Boxes are 90x300, so a 12px step is 0.04
    of the box height — exactly the motion threshold."""
    ctx, tracks, idents = bufs
    now = clock.now()
    _FID[0] += 1
    fid = _FID[0]
    out = []
    for tid, x in people.items():
        out.append(Track(track_id=tid, camera_id="camA", frame_id=fid,
                         timestamp=now,
                         bbox=BoundingBox(x1=float(x), y1=100.0,
                                          x2=float(x + 90), y2=400.0),
                         confidence=0.9))
        idents.update(Identity(track_id=tid, camera_id="camA", frame_id=fid,
                               timestamp=now, recipient_id=("ravi" if tid in targets
                                                            else None),
                               is_target=tid in targets, confidence=0.9))
    tracks.update(TrackResult(camera_id="camA", frame_id=fid, timestamp=now,
                              tracks=out))


print("\n1. a MOVING visitor triggers snapshots")
rule = VisitorRule()
bufs = _world()
events = []
for k in range(8):
    _tick(bufs, {7: 100 + k * 30})            # walking across frame
    events += rule.evaluate(bufs[0])
    time.sleep(0.02)
kinds = [e.event_type for e in events]
check("visitor_motion_snapshot fired", "visitor_motion_snapshot" in kinds, str(kinds))
check("it carries the track", events and events[0].track_id == 7)
check("and NO recipient_id (a visitor has none)",
      events and events[0].recipient_id is None)


print("\n2. a STILL visitor does NOT (this is the whole v1 bug)")
rule2 = VisitorRule()
bufs2 = _world()
still = []
for k in range(10):
    _tick(bufs2, {7: 100})                     # parked on the sofa
    still += rule2.evaluate(bufs2[0])
    time.sleep(0.02)
check("no snapshots for a motionless visitor", not still,
      str([e.event_type for e in still]))


print("\n3. the RECIPIENT is never a visitor")
rule3 = VisitorRule()
bufs3 = _world()
mine = []
for k in range(8):
    _tick(bufs3, {1: 100 + k * 30}, targets=(1,))
    mine += rule3.evaluate(bufs3[0])
    time.sleep(0.02)
check("the target moving raises nothing here", not mine,
      str([e.event_type for e in mine]))


print("\n4. an UNIDENTIFIED person IS a visitor (v1 required an identity)")
rule4 = VisitorRule()
ctx4, tracks4, _ = _world()
for k in range(8):
    now = clock.now()
    _FID[0] += 1
    tracks4.update(TrackResult(
        camera_id="camA", frame_id=_FID[0], timestamp=now,
        tracks=[Track(track_id=42, camera_id="camA", frame_id=_FID[0],
                      timestamp=now,
                      bbox=BoundingBox(x1=float(100 + k * 30), y1=100.0,
                                       x2=float(190 + k * 30), y2=400.0),
                      confidence=0.9)]))
    got = rule4.evaluate(ctx4)
    if got:
        break
    time.sleep(0.02)
check("a track with NO identity record still counts", bool(got),
      "v1 skipped these — exactly the people it most needed to capture")


print("\n5. two visitors share the budget fairly (they take turns)")
rule5 = VisitorRule()
bufs5 = _world()
both, per_tick = [], []
for k in range(8):
    _tick(bufs5, {7: 100 + k * 30, 8: 400 + k * 30})
    got = rule5.evaluate(bufs5[0])
    both += got
    per_tick.append(len(got))
    time.sleep(0.03)
check("both visitors get captured, not one of them forever",
      {e.track_id for e in both} == {7, 8}, str({e.track_id for e in both}))
check("never more than one snapshot in a tick", max(per_tick) <= 1, str(per_tick))


print("\n6. ONE snapshot per interval PER CAMERA")
rule6 = VisitorRule()
bufs6 = _world()
settings.visitor_snapshot_interval_secs = 999.0
try:
    burst = []
    for k in range(10):
        # two visitors walking AND the tracker re-numbering one of them every
        # 3 ticks (an ID switch mints a fresh never-snapped track)
        _tick(bufs6, {7: 100 + k * 30, 100 + k // 3: 400 + k * 30})
        # a third visitor walking in ANOTHER room, same minute
        now = clock.now()
        bufs6[1].update(TrackResult(
            camera_id="camB", frame_id=_FID[0], timestamp=now,
            tracks=[Track(track_id=5, camera_id="camB", frame_id=_FID[0],
                          timestamp=now,
                          bbox=BoundingBox(x1=float(100 + k * 30), y1=100.0,
                                           x2=float(190 + k * 30), y2=400.0),
                          confidence=0.9)]))
        burst += rule6.evaluate(bufs6[0])
        time.sleep(0.02)
    per_cam = {c: sum(e.camera_id == c for e in burst) for c in ("camA", "camB")}
    check("exactly one on camA, however many visitors / track ids",
          per_cam["camA"] == 1, str(per_cam))
    check("the other camera gets its own one, not blocked by camA",
          per_cam["camB"] == 1, str(per_cam))
finally:
    settings.visitor_snapshot_interval_secs = 0.05

from config.settings import Settings                       # noqa: E402
check("the shipped default is one per minute per camera",
      Settings.model_fields["visitor_snapshot_interval_secs"].default == 60.0)


print("\n7. the event survives the cloud recipient gate")
ROOT = pathlib.Path(__file__).resolve().parents[2]
pub = io.open(ROOT / "edge/alerts/cloud_alert_publisher.py", encoding="utf-8").read()
check("a non-recipient exemption exists", "_NON_RECIPIENT_TYPES" in pub)
# The set itself lives in the enricher and is IMPORTED here — two copies of the
# same list drift the moment one is edited, so assert the import, not a literal.
from events.event_enricher import NON_RECIPIENT_TYPES as _NRT   # noqa: E402
check("visitor_motion_snapshot is in it", "visitor_motion_snapshot" in _NRT,
      str(_NRT))
check("the gate consults it", "etype not in _NON_RECIPIENT_TYPES" in pub)

enr = io.open(ROOT / "edge/events/event_enricher.py", encoding="utf-8").read()
check("the enricher titles it", '"visitor_motion_snapshot"' in enr)
check("as info, never an alert",
      '"visitor_motion_snapshot": ("info"' in enr)
check("it is a cloud SNAPSHOT type",
      "visitor_motion_snapshot" in settings.cloud_snapshot_event_types)
check("and NOT an alert type",
      "visitor_motion_snapshot" not in settings.cloud_alert_event_types)

eng = io.open(ROOT / "edge/rules/rule_engine.py", encoding="utf-8").read()
check("the rule is actually registered in the engine", "VisitorRule()" in eng)


print("\n8. one frame produces ONE snapshot naming BOTH people")
from rules.rule_engine import RuleEngine                       # noqa: E402
from schemas.event import Event                                # noqa: E402


def _evt(kind, cam="camA", tid=1):
    return Event(event_id=kind, event_type=kind, camera_id=cam,
                 room_name="", timestamp=clock.now_iso(), track_id=tid)


merged = RuleEngine._merge_same_frame([
    _evt("standing_up", "camA", 1),
    _evt("visitor_motion_snapshot", "camA", 7),
])
kinds = [e.event_type for e in merged]
check("the recipient event survives", "standing_up" in kinds, str(kinds))
check("the duplicate visitor snapshot of the SAME frame is dropped",
      "visitor_motion_snapshot" not in kinds, str(kinds))

other_cam = RuleEngine._merge_same_frame([
    _evt("standing_up", "camA", 1),
    _evt("visitor_motion_snapshot", "camB", 7),
])
check("a visitor on ANOTHER camera is kept",
      len(other_cam) == 2, str([e.event_type for e in other_cam]))

alone = RuleEngine._merge_same_frame([_evt("visitor_motion_snapshot", "camA", 7)])
check("a visitor alone is never dropped", len(alone) == 1)


print("\n9. the annotation names who else was there")
from events.event_enricher import EventEnricher, NON_RECIPIENT_TYPES  # noqa: E402

ctx9, tracks9, idents9 = _world()
_tick((ctx9, tracks9, idents9), {1: 100, 7: 400}, targets=(1,))
enr = EventEnricher.__new__(EventEnricher)

phrase = enr._co_present(_evt("standing_up", "camA", 1), ctx9)
check("a recipient event names the visitor", phrase == "with a visitor", str(phrase))

phrase = enr._co_present(_evt("visitor_motion_snapshot", "camA", 7), ctx9)
check("a visitor event mentions the recipient — by role, never by name",
      phrase == "with the care recipient", str(phrase))

ctx10, tracks10, idents10 = _world()
_tick((ctx10, tracks10, idents10), {1: 100}, targets=(1,))
check("alone in frame -> no co-presence phrase",
      enr._co_present(_evt("standing_up", "camA", 1), ctx10) is None)

ctx11, tracks11, idents11 = _world()
_tick((ctx11, tracks11, idents11), {1: 100, 7: 400, 8: 500}, targets=(1,))
check("two visitors are counted, not listed",
      enr._co_present(_evt("standing_up", "camA", 1), ctx11) == "with 2 visitors")


print("\n10. the cloud line never names anyone")
from types import SimpleNamespace                                    # noqa: E402
from alerts.alert_format import describe                             # noqa: E402

line = describe(SimpleNamespace(
    event_type="visitor_motion_snapshot", title="Visitor moving",
    room_name="LOUNGE", zone_name=None, detail=None,
    co_present="with the care recipient",
    timestamp="2026-09-09T08:33:00+05:30"))
check("a visitor line is about the visitor, in the room",
      line.startswith("Visitor moving in Lounge"), line)
check("co-presence rides into the line", "with the care recipient" in line, line)
pub2 = io.open(ROOT / "edge/alerts/cloud_alert_publisher.py", encoding="utf-8").read()
check("the cloud sends the ONE wording", "describe(event)" in pub2)
check("no first name is read for the text", "firstName" not in pub2)
check("the type set is IMPORTED, not re-listed",
      "from events.event_enricher import NON_RECIPIENT_TYPES" in pub2)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll visitor-snapshot checks passed.")
