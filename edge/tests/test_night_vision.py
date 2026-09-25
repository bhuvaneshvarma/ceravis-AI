"""
Night vision — the camera switches to INFRARED and the stack must follow.

What this pins down, each in BOTH directions (the night rule must act at night
AND leave the daytime path exactly as it was):

  1. the modality of each camera is read correctly from the picture — colour vs
     infrared, a tinted monochrome still infrared, a dead band that holds, no
     flapping — and the camera's own ONVIF IrCutFilter settles only the dead band;
  2. the ONVIF read is read-only and tells "not reported" (Tapo) from
     "unreachable";
  3. the gallery matches a night query against night vectors and a day query
     against exactly the day vectors it always saw;
  4. the lock holds a recipient the model cannot see in the dark, releases one
     that is CONTRADICTED, re-joins a person re-detected in place, and takes the
     only person in the home as the recipient — only at night, only then;
  5. only a strongly-verified lock carried on the same track teaches the IR
     gallery or the negative pool;
  6. the recipient walking about in the dark is not reported as a visitor;
  7. a colour<->infrared switch neither fakes a movement nor advances no_motion;
  8. IR sensor noise cannot pass a smear through the crop-quality gate;
  9. the stores and the tracker keep colour and infrared apart.

Run:  PYTHONPATH=edge python edge/tests/test_night_vision.py
"""
import sys
import tempfile
import time
import types
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import cv2

from config.settings import settings

settings.visitor_snapshot_interval_secs = 0.05      # test scale
settings.night_context_min_track_secs = 0.05

from common import clock                                   # noqa: E402
from detection.detection_schema import BoundingBox         # noqa: E402
from ingestion import illumination                         # noqa: E402
from ingestion.illumination import Modality                # noqa: E402
from pose.posture_buffer import PostureBuffer              # noqa: E402
from reid import crop_quality                              # noqa: E402
from reid.faiss_index import MatchResult                   # noqa: E402
from reid.identity_buffer import IdentityBuffer            # noqa: E402
from reid.identity_schema import Identity                  # noqa: E402
from reid.recency_buffer import RecencyBuffer              # noqa: E402
from reid.target_lock import NightContext, TargetLockManager  # noqa: E402
from config import scene_rules  # noqa: E402
from reid.track_memory import TrackMemory                  # noqa: E402
from rules.rule_context import RuleContext                 # noqa: E402
from rules.target_motion import TargetMotionDetector       # noqa: E402
from rules.visitor_rule import VisitorRule                 # noqa: E402
from tracking.track_buffer import TrackBuffer              # noqa: E402
from tracking.track_schema import Track, TrackResult       # noqa: E402


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


RNG = np.random.default_rng(7)


# ---- synthetic pictures ----------------------------------------------------

def colour_frame() -> np.ndarray:
    """A lit room: blocks of different colours."""
    f = np.zeros((360, 640, 3), np.uint8)
    for i in range(8):
        for j in range(4):
            f[j * 90:(j + 1) * 90, i * 80:(i + 1) * 80] = RNG.integers(40, 220, 3)
    return f


def ir_frame(tint=(0, 0, 0)) -> np.ndarray:
    """What the camera streams at night: luminance only (+ an optional UNIFORM
    tint, which is a cast, not colour)."""
    g = np.zeros((360, 640), np.uint8)             # a room: things of different
    for i in range(8):                              # IR brightness, plus sensor noise
        for j in range(4):
            g[j * 90:(j + 1) * 90, i * 80:(i + 1) * 80] = RNG.integers(30, 220)
    g = cv2.GaussianBlur(g, (7, 7), 2)
    f = cv2.merge([g, g, g]).astype(np.int16) + np.array(tint, np.int16)
    f += RNG.integers(-6, 7, (360, 640, 1), dtype=np.int16)
    return np.clip(f, 0, 255).astype(np.uint8)


def band_frame(d: int = 5) -> np.ndarray:
    """Barely-coloured: chroma wobbling +-d around neutral — inside the dead
    band (the 160-px measurement averages pixel-level chroma noise down, which is
    exactly what keeps infrared sensor noise from reading as colour)."""
    y = np.zeros((360, 640), np.uint8)             # the same kind of room
    for i in range(8):
        for j in range(4):
            y[j * 90:(j + 1) * 90, i * 80:(i + 1) * 80] = RNG.integers(60, 200)
    sign = np.where(RNG.random((360, 640)) < 0.5, -1, 1)
    cr = np.clip(128 + d * sign, 0, 255).astype(np.uint8)
    cb = np.clip(128 - d * sign, 0, 255).astype(np.uint8)
    return cv2.cvtColor(cv2.merge([y, cr, cb]), cv2.COLOR_YCrCb2BGR)


# =============================================================================
print("\n1. modality is read from the picture")
illumination.reset()
c, _ = illumination.measure(colour_frame())
i, _ = illumination.measure(ir_frame())
t, _ = illumination.measure(ir_frame(tint=(18, -6, 20)))
b, _ = illumination.measure(band_frame())
check("a lit colour room measures well above the colour bar",
      c is not None and c >= settings.illumination_color_min_chroma, f"{c:.1f}")
check("an infrared frame measures ~0", i is not None
      and i <= settings.illumination_ir_max_chroma, f"{i:.2f}")
check("a UNIFORM tint on monochrome is still infrared (a cast is not colour)",
      t is not None and t <= settings.illumination_ir_max_chroma, f"{t:.2f}")
check("a barely-coloured frame lands in the dead band",
      b is not None and settings.illumination_ir_max_chroma < b
      < settings.illumination_color_min_chroma, f"{b:.2f}")
dark = np.full((360, 640, 3), 4, np.uint8)
check("a black frame cannot be judged (None, not a guess)",
      illumination.measure(dark)[0] is None)
m = illumination.monochrome(colour_frame())
check("monochrome() yields three identical channels",
      np.array_equal(m[..., 0], m[..., 1]) and np.array_equal(m[..., 1], m[..., 2]))

grey = np.full((720, 1280, 3), 128, np.uint8)             # decoder concealment
check("a flat decoder-grey frame has no modality (not infrared)",
      illumination.measure(grey)[0] is None)
part = colour_frame().copy()
part[: part.shape[0] * 3 // 4] = 128                        # 3/4 concealed
check("a mostly-concealed frame has no modality either",
      illumination.measure(part)[0] is None)
illumination.reset()
illumination.observe("gcam", colour_frame())
for _ in range(settings.illumination_confirm_samples + 2):
    illumination.observe("gcam", grey)
check("a colour camera fed grey frames stays colour", not illumination.is_ir("gcam"))
illumination.reset()

print("\n2. hysteresis: decide at once, switch only on sustained evidence")
illumination.reset()
check("unknown camera reads as COLOUR (the daytime default)",
      illumination.modality("cam") is Modality.COLOR and illumination.epoch("cam") == 0)
illumination.observe("cam", colour_frame())
check("first decisive sample decides immediately",
      illumination.modality("cam") is Modality.COLOR and illumination.epoch("cam") == 1)
n = settings.illumination_confirm_samples
for _ in range(n - 1):
    illumination.observe("cam", ir_frame())
check(f"{n - 1} infrared samples are not enough to switch",
      not illumination.is_ir("cam"))
illumination.observe("cam", ir_frame())
check(f"the {n}th switches to NIGHT VISION and bumps the epoch",
      illumination.is_ir("cam") and illumination.epoch("cam") == 2)
for k in range(12):                                  # headlight / lamp flicker
    illumination.observe("cam", colour_frame() if k % 2 else ir_frame())
check("alternating evidence never flaps the mode",
      illumination.is_ir("cam") and illumination.epoch("cam") == 2)
for _ in range(n + 2):
    illumination.observe("cam", band_frame())
check("the dead band HOLDS the current mode", illumination.is_ir("cam"))
for _ in range(n):
    illumination.observe("cam", dark)
check("an unjudgeable frame is no evidence either way", illumination.is_ir("cam"))
illumination.observe("cam", ir_frame())
d = illumination.describe("cam")
check("describe() reports the mode and the evidence",
      d["night_vision"] is True and d["modality"] == "ir"
      and d["decided_by"] == "image" and d["chroma"] is not None)

print("\n3. the camera's ONVIF IrCutFilter settles ONLY the dead band")
illumination.reset()
illumination.observe("c2", colour_frame())
illumination.set_onvif_ir_cut("c2", "OFF")           # camera: filter out = IR
for _ in range(n):
    illumination.observe("c2", band_frame())
check("dead band + camera says OFF -> infrared, decided by the camera",
      illumination.is_ir("c2") and illumination.state("c2").decided_by == "camera")
illumination.set_onvif_ir_cut("c2", "OFF")
for _ in range(n):
    illumination.observe("c2", colour_frame())
check("a decisively COLOUR picture overrides the camera's claim",
      not illumination.is_ir("c2"))
illumination.set_onvif_ir_cut("c2", "AUTO")
for _ in range(n + 2):
    illumination.observe("c2", band_frame())
check("AUTO states nothing about now: dead band holds",
      not illumination.is_ir("c2"))
illumination.reset()


# =============================================================================
print("\n4. ONVIF read: IrCutFilter, read-only, 'not reported' vs 'unreachable'")
import onvif.client as oc                                  # noqa: E402
from onvif.soap import OnvifError                          # noqa: E402

_real_call = oc.call
sent: list[str] = []


def fake_call(mode, *, fault=False, down=False):
    def call(url, body, user="", pw="", timeout=6.0):
        sent.append(body)
        if down:
            raise OnvifError("cannot reach camera")
        if "GetServices" in body or "GetCapabilities" in body:
            raise OnvifError("SOAP fault: not supported")   # Tapo does this
        if "GetVideoSources" in body:
            return ElementTree.fromstring(
                "<Body><GetVideoSourcesResponse><VideoSources token='vs1'/>"
                "</GetVideoSourcesResponse></Body>")
        if "GetImagingSettings" in body:
            if fault:
                raise OnvifError("SOAP fault: ActionNotSupported")
            ir = f"<IrCutFilter>{mode}</IrCutFilter>" if mode else ""
            return ElementTree.fromstring(
                "<Body><GetImagingSettingsResponse><ImagingSettings>"
                f"<Brightness>50</Brightness>{ir}"
                "</ImagingSettings></GetImagingSettingsResponse></Body>")
        raise AssertionError(body)
    return call


try:
    oc.call = fake_call("AUTO")
    check("a camera reporting IrCutFilter AUTO -> 'AUTO'",
          oc.OnvifCamera("http://x/onvif/service", "u", "p").ir_cut_filter() == "AUTO")
    oc.call = fake_call("OFF")
    check("... OFF -> 'OFF'",
          oc.OnvifCamera("http://x", "u", "p").ir_cut_filter() == "OFF")
    oc.call = fake_call(None)
    check("Tapo-shaped settings (no IrCutFilter) -> None, not an error",
          oc.OnvifCamera("http://x", "u", "p").ir_cut_filter() is None)
    oc.call = fake_call("ON", fault=True)
    check("imaging not offered (SOAP fault) -> None",
          oc.OnvifCamera("http://x", "u", "p").ir_cut_filter() is None)
    oc.call = fake_call("ON", down=True)
    try:
        oc.OnvifCamera("http://x", "u", "p").ir_cut_filter()
        check("an unreachable camera raises (so it is asked again)", False)
    except OnvifError:
        check("an unreachable camera raises (so it is asked again)", True)
    check("nothing ever WRITES to the camera",
          sent and not any(b.lstrip().startswith("<Set") for b in sent))
finally:
    oc.call = _real_call


# =============================================================================
print("\n5. gallery: a night query sees night vectors; a day query, day vectors")
if "faiss" not in sys.modules:                  # dev box: a stand-in index
    class _Flat:
        def __init__(self, d):
            self.ntotal = 0

        def add(self, x):
            self.ntotal += len(x)
    sys.modules["faiss"] = types.SimpleNamespace(IndexFlatIP=_Flat)
    import reid.faiss_index as fi                          # noqa: E402
    fi.faiss, fi._FAISS_AVAILABLE = sys.modules["faiss"], True
from reid.faiss_index import FaissGallery                  # noqa: E402

D = settings.reid_embedding_dim


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def axis(*parts):
    v = np.zeros(D, np.float32)
    for k, c in parts:
        v[k] = c
    return unit(v)


DAY_A, NIGHT_A, DAY_B = axis((0, 1.0)), axis((1, 1.0)), axis((2, 1.0))
g = FaissGallery(D)
g.rebuild(np.stack([DAY_A, NIGHT_A, DAY_B]), ["ravi", "ravi", "bob"],
          ["", "", ""], ["color", "ir", "color"])
check("counts per modality", g.counts() == {"color": 2, "ir": 1}, str(g.counts()))
check("recipient_ids()", sorted(g.recipient_ids()) == ["bob", "ravi"])
m = g.match(DAY_A)
check("day query (no modality arg) matches the colour look — as always",
      m.is_match and m.recipient_id == "ravi")
m = g.match(NIGHT_A)
check("a day query never sees the IR vectors", not m.is_match, f"{m.score:.2f}")
m = g.match(NIGHT_A, modality="ir")
check("a night query matches the IR look", m.is_match and m.recipient_id == "ravi")
m = g.match(DAY_B, modality="ir")
check("a recipient with NO IR vector falls back to all of theirs",
      m.is_match and m.recipient_id == "bob")


# =============================================================================
print("\n6. the lock at night: hold / contradiction / rejoin / context")
TARGET, OTHER, BLIND = axis((0, 1.0)), axis((2, 1.0)), axis((5, 1.0))


class _Gallery:
    """ravi at TARGET, bob at OTHER, same vectors in both modalities. A BLIND
    feature (what infrared often gives) matches nobody."""

    def __init__(self):
        self.calls: list[str] = []

    def match(self, feat, modality="color"):
        self.calls.append(modality)
        s_r, s_b = float(feat @ TARGET), float(feat @ OTHER)
        thr = scene_rules.for_ir(modality == "ir").reid_match_threshold
        if s_r >= s_b:
            return MatchResult("ravi", s_r, None, s_r - s_b,
                               s_r >= thr and s_r - s_b >= settings.reid_match_margin)
        return MatchResult("bob", s_b, None, s_b - s_r,
                           s_b >= thr and s_b - s_r >= settings.reid_match_margin)


NIGHT = NightContext(ir=True)
BOX = (100.0, 100.0, 190.0, 400.0)


def locked(gal=None, memory=None):
    mgr = TargetLockManager(gal or _Gallery(), recency=RecencyBuffer(),
                            memory=memory)
    out = mgr.update("cam", {1: BOX}, lambda t: TARGET)        # daylight acquire
    assert out.target_track_id == 1 and out.basis == "verified"
    return mgr


mgr = locked()
held = [mgr.update("cam", {1: BOX}, lambda t: BLIND, NIGHT) for _ in range(6)]
check("night, blind, uncontradicted: HELD tick after tick",
      all(o.target_track_id == 1 and not o.released for o in held))
check("... basis 'continuity', still trusted (strongly verified on this track)",
      held[-1].basis == "continuity" and held[-1].trusted)
check("... and approved for IR learning in solitude",
      held[-1].adaptive is not None and held[-1].learn_ir)

mgr = locked()
day = [mgr.update("cam", {1: BOX}, lambda t: BLIND) for _ in range(3)]
check("DAY, same blind look: released exactly as before",
      any(o.released for o in day))

mgr = locked()
cont = [mgr.update("cam", {1: BOX}, lambda t: OTHER, NIGHT) for _ in range(3)]
check("night, but another enrolled person MATCHES: released (contradiction)",
      any(o.released for o in cont))

mem = TrackMemory()
for _ in range(3):
    mem.add_negative(BLIND)
mgr = locked(memory=mem)
neg = [mgr.update("cam", {1: BOX}, lambda t: BLIND, NIGHT) for _ in range(3)]
check("night, looks like a KNOWN non-target: released (contradiction)",
      any(o.released for o in neg))

gal = _Gallery()
mgr = locked(gal)
mgr.update("cam", {1: BOX}, lambda t: BLIND, NIGHT)
check("night matching uses the infrared gallery", gal.calls[-1] == "ir")
check("day matching makes the historical call", gal.calls[0] == "color")

# rejoin — the locked track vanishes, one person re-detected in place
mgr = locked()
mgr.update("cam", {1: BOX}, lambda t: BLIND, NIGHT)
near = (110.0, 105.0, 200.0, 405.0)
o = mgr.update("cam", {9: near}, lambda t: BLIND, NIGHT)
check("night: one person re-detected where the recipient was -> rejoined",
      o.target_track_id == 9 and o.basis == "continuity" and not o.trusted)
check("... a NEW track is never learnable", o.adaptive is None and not o.learn_ir)
mgr = locked()
o = mgr.update("cam", {9: near}, lambda t: BLIND)
check("day: the same blind reappearance is NOT taken (lost, search widens)",
      o.target_track_id is None and o.lost)
mgr = locked()
o = mgr.update("cam", {9: near, 10: (400.0, 100.0, 490.0, 400.0)},
               lambda t: BLIND, NIGHT)
check("night: two people reappear -> no rejoin (ambiguous)",
      o.target_track_id is None)

# context — the only person in the home
SOLE = NightContext(ir=True, sole_recipient="ravi")
MOVED = (160.0, 100.0, 250.0, 400.0)                  # walked 60 px (0.2 x height)
JITTER = (103.0, 98.0, 192.0, 403.0)                  # a phantom's box wobble
mgr = TargetLockManager(_Gallery(), recency=RecencyBuffer(), memory=TrackMemory())
o = mgr.update("cam", {3: BOX}, lambda t: None, SOLE)
check("context: not on the first sighting (must be seen steadily)",
      o.target_track_id is None)
time.sleep(settings.night_context_min_track_secs + 0.02)
for _ in range(20):
    o = mgr.update("cam", {3: JITTER}, lambda t: None, SOLE)
    o = mgr.update("cam", {3: BOX}, lambda t: None, SOLE)
check("context: a steady phantom that NEVER moves is never the recipient",
      o.target_track_id is None)
o = mgr.update("cam", {3: MOVED}, lambda t: None, SOLE)
check("context: lone person who moved, no usable look (blanket) -> recipient",
      o.target_track_id == 3 and o.recipient_id == "ravi" and o.basis == "context")
check("... a context lock teaches NOTHING (no learning, not trusted)",
      o.adaptive is None and not o.learn_ir and not o.trusted)
o = mgr.update("cam", {3: BOX}, lambda t: BLIND, SOLE)
check("... and is then held on continuity, still labelled 'context'",
      o.target_track_id == 3 and o.basis == "context")

mgr = TargetLockManager(_Gallery(), recency=RecencyBuffer(), memory=TrackMemory())
mgr.update("cam", {3: BOX}, lambda t: OTHER, SOLE)
time.sleep(settings.night_context_min_track_secs + 0.02)
o = mgr.update("cam", {3: MOVED}, lambda t: OTHER, SOLE)
check("context refused when the look CONTRADICTS (matches bob)",
      o.target_track_id != 3 or o.recipient_id != "ravi")

for label, night in (("day", None), ("night w/o sole_recipient", NightContext(True))):
    mgr = TargetLockManager(_Gallery(), recency=RecencyBuffer())
    mgr.update("cam", {3: BOX}, lambda t: None, night)
    time.sleep(settings.night_context_min_track_secs + 0.02)
    o = mgr.update("cam", {3: MOVED}, lambda t: None, night)
    check(f"no context identity: {label}", o.target_track_id is None)

# huddle at night: identity not re-verified -> stops learning
mgr = locked()
other = (120.0, 100.0, 210.0, 400.0)                  # right next to the target
mgr.update("cam", {1: BOX, 2: other}, lambda t: BLIND, NIGHT)     # freeze
o = mgr.update("cam", {1: BOX}, lambda t: BLIND, NIGHT)           # separated
check("after a night huddle with no re-verification: held but NOT learnable",
      o.target_track_id == 1 and not o.learn_ir and not o.trusted)


# =============================================================================
print("\n7. recency never compares colour looks with an infrared query")
rb = RecencyBuffer()
rb.push("ravi", DAY_A)
check("colour memory, infrared query -> no memory (gallery alone, no veto)",
      rb.score("ravi", NIGHT_A, "ir") is None)
check("colour memory still serves a colour query", rb.score("ravi", DAY_A) > 0.99)
rb.push("ravi", NIGHT_A, "ir")
check("infrared memory serves an infrared query",
      rb.score("ravi", NIGHT_A, "ir") > 0.99)


# =============================================================================
print("\n8. the recipient in the dark is not reported as a visitor")
illumination.reset()


def _vworld():
    tracks, idents, postures = TrackBuffer(), IdentityBuffer(), PostureBuffer()
    ctx = RuleContext(frames=None, detections=None, tracks=tracks, poses=None,
                      postures=postures, posture_tracker=None, identities=idents)
    return ctx, tracks, idents


def _walk(bufs, cam, step, target_cam=None):
    ctx, tracks, idents = bufs
    now = clock.now()
    tracks.update(TrackResult(camera_id=cam, frame_id=step, timestamp=now, tracks=[
        Track(track_id=7, camera_id=cam, frame_id=step, timestamp=now,
              bbox=BoundingBox(x1=100.0 + step * 30, y1=100.0,
                               x2=190.0 + step * 30, y2=400.0), confidence=0.9)]))
    if target_cam:
        tracks.update(TrackResult(camera_id=target_cam, frame_id=step,
                                  timestamp=now, tracks=[
            Track(track_id=1, camera_id=target_cam, frame_id=step, timestamp=now,
                  bbox=BoundingBox(x1=10.0, y1=10.0, x2=100.0, y2=300.0),
                  confidence=0.9)]))
        idents.update(Identity(track_id=1, camera_id=target_cam, frame_id=step,
                               timestamp=now, recipient_id="ravi", is_target=True,
                               confidence=0.9))


def _visitor_events(cam_ir: bool, target_cam=None) -> int:
    illumination.reset()
    for _ in range(settings.illumination_confirm_samples + 1):
        illumination.observe("hall", ir_frame() if cam_ir else colour_frame())
    rule, bufs, n = VisitorRule(), _vworld(), 0
    for step in range(8):
        _walk(bufs, "hall", step, target_cam)
        n += len(rule.evaluate(bufs[0]))
        time.sleep(0.02)
    return n


check("colour camera, recipient nowhere: a walking stranger IS a visitor",
      _visitor_events(False) > 0)
check("infrared camera, recipient nowhere: held (could be the recipient)",
      _visitor_events(True) == 0)
check("infrared camera, recipient located elsewhere: IS a visitor",
      _visitor_events(True, target_cam="bedroom") > 0)
illumination.reset()


# =============================================================================
print("\n9. a colour<->infrared switch neither fakes movement nor advances no_motion")


class _Box:
    x1, y1, x2, y2 = 200.0, 100.0, 300.0, 340.0
    width, height = 100.0, 240.0


day_scene = colour_frame()
night_scene = ir_frame()
det = TargetMotionDetector()
t0 = clock.now()
from datetime import timedelta                              # noqa: E402
k = 0


def step(frame, epoch):
    global k
    k += 1
    noisy = np.clip(frame.astype(np.int16) + RNG.integers(-2, 3, frame.shape),
                    0, 255).astype(np.uint8)
    return det.update("cam", None, _Box(), noisy, t0 + timedelta(seconds=k),
                      scene_epoch=epoch)


for _ in range(settings.pixel_noise_min_samples + 10):
    v = step(day_scene, 1)
still_before = v.still_secs
check("daylight: still, accruing", not v.moving and still_before > 5, f"{still_before}")
v = step(night_scene, 2)                                     # IR-cut clicks
check("the switch is NOT a movement", not v.moving, v.reason)
check("... and the clock is HELD, not reset", v.still_secs == still_before,
      f"{v.still_secs} vs {still_before}")
held = [step(night_scene, 2) for _ in range(settings.pixel_noise_min_samples - 2)]
check("while re-learning the night scene: held, never moving",
      all((not h.moving) and h.still_secs == still_before for h in held))
for _ in range(6):
    v = step(night_scene, 2)
check("re-learned: accruing again from where it was held",
      not v.moving and v.still_secs > still_before, f"{v.still_secs}")

det2 = TargetMotionDetector()
k = 0
for _ in range(settings.pixel_noise_min_samples + 10):
    det2.update("cam", None, _Box(), day_scene, t0 + timedelta(seconds=k))
    k += 1
ctl = [det2.update("cam", None, _Box(), night_scene, t0 + timedelta(seconds=k + j))
       for j in range(1, 4)]
check("(control) without the epoch the same switch reads as a MOVEMENT "
      "and restarts the clock", any(c.moving for c in ctl),
      ", ".join(c.reason for c in ctl))


# =============================================================================
print("\n10. infrared sensor noise cannot pass a smear through the crop gate")


class _B:
    x1, y1, x2, y2 = 100.0, 100.0, 196.0, 340.0


smear = cv2.GaussianBlur(cv2.merge([np.full((240, 96), 128, np.uint8)] * 3),
                         (9, 9), 3)
smear = np.clip(smear.astype(np.float32)
                + RNG.normal(0, 6, smear.shape[:2])[..., None], 0, 255).astype(np.uint8)
body = np.full((240, 96), 90, np.uint8)
body[:, ::12] = 200
body[::20, :] = 40
body = cv2.merge([body] * 3)
day_q = crop_quality.assess(smear, _B(), 1920, 1080, 0.9)
ir_q = crop_quality.assess(smear, _B(), 1920, 1080, 0.9, ir=True)
check("(the flaw) the daytime gate calls a noisy smear sharp", day_q.ok,
      f"lap {day_q.sharpness:.0f}")
check("the IR gate rejects the same smear", not ir_q.ok, ir_q.reason)
check("the IR gate passes a genuinely sharp body",
      crop_quality.assess(body, _B(), 1920, 1080, 0.9, ir=True).ok)
check("the daytime reason text is unchanged",
      crop_quality.assess(np.full((240, 96, 3), 128, np.uint8), _B(), 1920,
                          1080).reason.startswith("too blurred (lap var"))


# =============================================================================
print("\n11. stores and tracker keep colour and infrared apart")
from enrollment import enrollment_manager as em_mod         # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    old = settings.data_dir
    settings.data_dir = tmp
    try:
        mgr = em_mod.EnrollmentManager()
        mgr.save_embeddings("ravi", np.stack([DAY_A, DAY_A]))
        mgr.save_embeddings("ravi", np.stack([NIGHT_A]), "ir")
        added_ir = mgr.append_adaptive("ravi", axis((3, 1.0)), "", cap=10,
                                       dedup_cos=0.92, modality="ir")
        dup_ir = mgr.append_adaptive("ravi", NIGHT_A, "", cap=10,
                                     dedup_cos=0.92, modality="ir")
        emb, ids, labels, mods = mgr.load_gallery()
        check("load_gallery returns per-vector modalities",
              mods == ["color", "color", "ir", "ir"] and len(ids) == 4, str(mods))
        check("IR adaptive dedups against IR looks only",
              added_ir and not dup_ir)
        body_dir = Path(tmp) / "recipients" / "ravi" / "body"
        check("colour files untouched by night vision (embeddings.npy = 2 rows)",
              np.load(body_dir / "embeddings.npy").shape[0] == 2
              and not (body_dir / "adaptive.npy").exists())
        st = mgr.embedding_stats("ravi")
        check("stats report the night gallery",
              st["enrolled_ir"] == 1 and st["adaptive_ir"] == 1
              and st["enrolled"] == 2 and st["adaptive"] == 0)
    finally:
        settings.data_dir = old

from tracking.botsort import BoTSORT                        # noqa: E402

bt = BoTSORT(track_high_thresh=0.5, track_low_thresh=0.1, new_track_thresh=0.6,
             match_thresh=0.8, track_buffer=30, proximity_thresh=0.5,
             appearance_thresh=0.25, with_reid=True, frame_rate=10.0)
xywh = np.array([[150.0, 250.0, 90.0, 300.0]], np.float32)
for _ in range(3):
    tracks = bt.update(xywh, np.array([0.9], np.float32), np.stack([DAY_A]))
tid = tracks[0].track_id
bt.reset_appearance()
check("reset_appearance forgets the look but keeps the track",
      bt.tracked_stracks and bt.tracked_stracks[0].smooth_feat is None
      and bt.tracked_stracks[0].track_id == tid)
tracks = bt.update(xywh, np.array([0.9], np.float32), np.stack([NIGHT_A]))
check("... the next look re-seeds it purely (no colour/IR blend)",
      tracks and tracks[0].track_id == tid
      and float(tracks[0].smooth_feat @ NIGHT_A) > 0.99)

from detection.detection_buffer import DetectionBuffer      # noqa: E402
from tracking.track_feature_buffer import TrackFeatureBuffer  # noqa: E402
from tracking.tracking_runner import TrackingRunner         # noqa: E402

illumination.reset()
fb = TrackFeatureBuffer()
tr = TrackingRunner(DetectionBuffer(), TrackBuffer(), feature_buffer=fb)
illumination.observe("cam", colour_frame())
check("tracker: colour camera -> not IR", tr._follow_light("cam") is False)
fb.update("cam", 1, DAY_A, DAY_A, 1, clock.now())
for _ in range(settings.illumination_confirm_samples):
    illumination.observe("cam", ir_frame())
check("tracker: after the switch -> IR", tr._follow_light("cam") is True)
check("... and the colour-era feature records are dropped", fb.get("cam", 1) is None)
illumination.reset()


print()
print("context identity needs the rest of the home to be EMPTY for a while")
from reid.reid_runner import ReIDRunner                     # noqa: E402
rr = ReIDRunner.__new__(ReIDRunner)
rr._targets = types.SimpleNamespace(all=lambda: {}, last_recipient=lambda: None)
rr._gallery = types.SimpleNamespace(recipient_ids=lambda: ["76"])
rr._last_person = {"LOUNGE": time.monotonic() - 3}
illumination.observe("LIVING", ir_frame())                  # the night rule set
check("someone seen next door 3 s ago -> no context identity",
      rr._sole_recipient("LIVING", {1: (0, 0, 10, 10)}) is None)
rr._last_person["LOUNGE"] = time.monotonic() - 30
check("next door empty for 30 s -> the lone person may be the recipient",
      rr._sole_recipient("LIVING", {1: (0, 0, 10, 10)}) == "76")
illumination.reset()
check("a colour camera (day rule set) never uses context identity",
      rr._sole_recipient("LIVING", {1: (0, 0, 10, 10)}) is None)

print()
print("two complete rule sets: night overrides, day untouched, the rest inherited")
check("day keep bar is the restored pre-night value",
      scene_rules.DAY.reid_match_threshold == settings.reid_match_threshold == 0.55)
check("night keep bar is its own", scene_rules.NIGHT.reid_match_threshold
      == settings.night_reid_match_threshold)
check("a rule with no night twin is inherited by the night set",
      scene_rules.NIGHT.reid_target_pick_margin == settings.reid_target_pick_margin)
check("policies: off by day, on at night",
      not scene_rules.DAY.lock_hold_uncontradicted
      and scene_rules.NIGHT.lock_hold_uncontradicted
      and not scene_rules.DAY.lock_context_identity
      and scene_rules.NIGHT.lock_context_identity)
_old = settings.night_reid_acquire_min_score
settings.night_reid_acquire_min_score = 0.91
check("tuning a night value moves only the night set",
      scene_rules.NIGHT.reid_acquire_min_score == 0.91
      and scene_rules.DAY.reid_acquire_min_score == settings.reid_acquire_min_score)
settings.night_reid_acquire_min_score = _old
illumination.observe("cam9", ir_frame())
check("an infrared camera runs the night set, an unknown one the day set",
      scene_rules.for_camera("cam9") is scene_rules.NIGHT
      and scene_rules.for_camera("nobody") is scene_rules.DAY)
illumination.reset()

print()
if failures:
    print(f"{len(failures)} night-vision check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All night-vision checks passed.")
