#!/usr/bin/env python3
"""
Sanity check for the main-stream height cap — the ONE code path that writes
configuration to a camera the customer owns.

The previous generation of this code inferred success from "the SOAP write did
not throw", and on one of the two bench models that inference was wrong: the
camera silently kept its old resolution (or clamped it) and the edge reported a
change that never happened. So the behaviour asserted here is:

  * the largest supported resolution AT OR BELOW the cap is chosen, from the
    camera's OWN reported options — never a guessed value
  * a camera already at or below the cap is NOT written to at all
  * only <Resolution> changes; bitrate, frame rate and GOP are written back
    exactly as read, so the same bits cover fewer pixels
  * the encoder is READ BACK, and a camera that ignored or clamped the write is
    reported as accepted=False rather than as a success
  * a cap below everything the camera offers falls to its smallest option and
    says so, instead of failing or writing something invalid

Both bench models are modelled from the real ONVIF dumps (C260: 4K main with
{2160,1440,1080,720}; C220: 1440p main with {1440,1080}).

No camera, no network — a fake SOAP transport answers every call.

    python tests/test_stream_profile.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))
os.chdir(EDGE)

import onvif.client as oc                                  # noqa: E402
from onvif.client import OnvifCamera                       # noqa: E402
from onvif.soap import OnvifError, strip_ns                # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


class FakeCamera:
    """A camera that answers the three encoder calls. `honours_write=False`
    models the real failure mode: the Set succeeds but nothing changes."""

    def __init__(self, options, current, honours_write=True,
                 bitrate=2560, fps=20, gov=25):
        self.options = options
        self.current = current
        self.honours_write = honours_write
        self.bitrate, self.fps, self.gov = bitrate, fps, gov
        self.writes: list[tuple[int, int]] = []
        self.last_set = ""

    def __call__(self, url, body, username="", password="", timeout=None):
        if "GetVideoEncoderConfigurationOptions" in body:
            res = "".join(f"<ResolutionsAvailable><Width>{w}</Width>"
                          f"<Height>{h}</Height></ResolutionsAvailable>"
                          for w, h in self.options)
            return ElementTree.fromstring(f"<Body><Options>{res}</Options></Body>")
        if "GetVideoEncoderConfiguration" in body:
            w, h = self.current
            return ElementTree.fromstring(f"""<Body><Configuration token="enc0">
                <Name>mainStream</Name><UseCount>2</UseCount><Encoding>H264</Encoding>
                <Resolution><Width>{w}</Width><Height>{h}</Height></Resolution>
                <Quality>4</Quality>
                <RateControl><FrameRateLimit>{self.fps}</FrameRateLimit>
                  <EncodingInterval>1</EncodingInterval>
                  <BitrateLimit>{self.bitrate}</BitrateLimit></RateControl>
                <H264><GovLength>{self.gov}</GovLength>
                  <H264Profile>Main</H264Profile></H264>
                <SessionTimeout>PT60S</SessionTimeout></Configuration></Body>""")
        if "SetVideoEncoderConfiguration" in body:
            self.last_set = body
            # the real transport strips namespaces before matching; do the same
            root = strip_ns(ElementTree.fromstring(f"<r>{body}</r>"))
            res = root.find(".//Resolution")
            want = (int(res.findtext("Width")), int(res.findtext("Height")))
            self.writes.append(want)
            if self.honours_write:
                self.current = want
            return ElementTree.fromstring("<Body><ok/></Body>")
        raise AssertionError("unexpected SOAP call: " + body[:60])


def camera(fake) -> OnvifCamera:
    oc.call = fake
    cam = OnvifCamera("http://192.168.0.250:2020/onvif/service", "admin", "pw")
    cam._resolved = True                      # skip service discovery
    cam._media_url = "http://192.168.0.250:2020/onvif/service"
    return cam


C260 = [(3840, 2160), (2560, 1440), (1920, 1080), (1280, 720)]
C220 = [(2560, 1440), (1920, 1080)]

# --------------------------------------------------------------------------
print("\n1. C260 (4K main) capped to 1440")
fake = FakeCamera(C260, (3840, 2160))
r = camera(fake).cap_stream_height("enc0", 1440)
check("picks the largest option at or below the cap", r["requested"] == "2560x1440")
check("reports what it was", r["before"] == "3840x2160")
check("read-back confirms the camera took it", r["after"] == "2560x1440")
check("reports changed + accepted", r["changed"] and r["accepted"])
check("exactly one write was issued", fake.writes == [(2560, 1440)])

# --------------------------------------------------------------------------
print("\n2. only the resolution is rewritten - the rest is preserved")
body = fake.last_set   # the Set request, not the read-back that followed
check("bitrate carried through untouched", "<BitrateLimit>2560</BitrateLimit>" in body)
check("frame rate carried through untouched", "<FrameRateLimit>20</FrameRateLimit>" in body)
check("GOP carried through untouched", "<GovLength>25</GovLength>" in body)
check("codec echoed back from the read, not forced", ">H264</Encoding>" in body)
check("the new resolution is what was sent",
      "<Width>2560</Width><Height>1440</Height>" in body)

# --------------------------------------------------------------------------
print("\n3. C220 is ALREADY 1440 -> nothing is written to the camera")
fake = FakeCamera(C220, (2560, 1440))
r = camera(fake).cap_stream_height("enc0", 1440)
check("no SOAP write was issued at all", fake.writes == [])
check("reported as unchanged", r["changed"] is False)
check("still reported as accepted (it is already compliant)", r["accepted"])
check("before and after agree", r["before"] == r["after"] == "2560x1440")

# --------------------------------------------------------------------------
print("\n4. a camera that IGNORES the write is reported honestly")
fake = FakeCamera(C260, (3840, 2160), honours_write=False)
r = camera(fake).cap_stream_height("enc0", 1440)
check("the write was attempted", fake.writes == [(2560, 1440)])
check("read-back shows the camera did NOT take it", r["after"] == "3840x2160")
check("accepted is False - no false success", r["accepted"] is False)
check("changed is False, because nothing actually changed", r["changed"] is False)

# --------------------------------------------------------------------------
print("\n5. a cap below everything the camera offers")
fake = FakeCamera(C220, (2560, 1440))
r = camera(fake).cap_stream_height("enc0", 240)
check("falls to the camera's smallest option", r["requested"] == "1920x1080")
check("and says what the camera can actually do", r["options"] == ["2560x1440", "1920x1080"])

# --------------------------------------------------------------------------
print("\n6. a camera that reports no resolutions at all raises, never guesses")
fake = FakeCamera([], (1920, 1080))
try:
    camera(fake).cap_stream_height("enc0", 1440)
    check("raises OnvifError", False)
except OnvifError:
    check("raises OnvifError", True)
    check("and wrote nothing", fake.writes == [])

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All stream-profile checks passed.")
