#!/usr/bin/env python3
"""
Every app-server call after sign-in is signed with the device key.

Checked over REAL HTTP against a stand-in app server that follows the rules the
Java side is held to — its saveSnapshot manifest built here independently of
the edge's own code:
  1. before its first sign-in a device sends nothing (it has no key);
  2. all eight calls — saveCamera, cameras/delete, deletePatientZoningFile,
     uploadEmbeddingFile, getPatientPostures, saveAlert, saveSnapshot,
     recordings/event — name the device first (deviceId), carry no API key,
     are signed with the device key and accepted; their answers come back as
     before (alertId, the posture list, 404 = nothing there);
  3. saveSnapshot is signed over its manifest: fields and files, any mix;
  4. saveAlert / saveSnapshot keep one Idempotency-Key across retries, each
     retry signed anew;
  5. a reply that is altered, unsigned, or a refusal (403: another patient) is
     never taken as delivered;
  6. the clock: a device clock 60 s off the server's is refused once, measured
     from the reply's Date header, signed again and accepted; small gaps are
     left alone;
  7. the wire log holds the request as sent (deviceId in it) and no key;
  8. posture images are fetched only from https links on Amazon S3.

    python tests/test_signed_calls.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-signed-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from config.settings import settings                              # noqa: E402
from common.device_identity import identity                       # noqa: E402
from integration import ceravis_api                               # noqa: E402
from integration.ceravis_api import CeravisApiError               # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


DEVICE = identity().name
DEVICE_KEY = "3c9f" * 16                       # a dummy device key, 64 hex
PATIENT = 5012
WINDOW = 5.0                                   # the server's timestamp window, seconds


def manifest(fields: dict, files: dict) -> bytes:
    """The saveSnapshot manifest as the Java side builds it (written here from
    the rule, not from the edge's code)."""
    lines = []
    for name in ("deviceId", "patientId", "text", "cameraNumber", "alertId", "category"):
        v = fields.get(name)
        lines.append(f"{name}=" + (hashlib.sha256(v.encode()).hexdigest() if v else ""))
    for name in ("image", "video"):
        v = files.get(name)
        lines.append(f"{name}=" + (hashlib.sha256(v).hexdigest() if v else ""))
    return "\n".join(lines).encode()


class AppServer:
    """The data calls as specified: timestamp within WINDOW of the server's own
    clock (skew = how far that clock is ahead), signature over the raw body or
    the manifest with the device's key, each signature once, the device acting
    only for its own patient; every 2xx reply signed."""

    def __init__(self) -> None:
        self.skew = 0.0
        self.used: set[str] = set()
        self.seen: list[dict] = []
        self.alerts: dict[str, int] = {}       # Idempotency-Key -> alertId
        self.tamper = self.unsigned = False
        self.zoning_stored = False

    def handle(self, method, path, headers, raw):
        ts = headers.get(ceravis_api.HDR_TIMESTAMP) or ""
        sig = headers.get(ceravis_api.HDR_SIGNATURE) or ""
        seen = {"path": path, "method": method, "api_key": headers.get("X-API-Key"),
                "idem": headers.get("Idempotency-Key"), "sig": sig}
        self.seen.append(seen)
        try:
            fresh = abs(time.time() + self.skew - float(ts)) <= WINDOW
        except ValueError:
            fresh = False
        if not fresh:
            return 401, None, {"code": 0, "data": None, "message": "Invalid or expired timestamp"}
        if headers.get("X-API-Key") or headers.get("X-Ceravis-Device"):
            return 400, None, {"code": 0, "data": None, "message": "no API key, no device header"}
        ctype = headers.get("Content-Type") or ""
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
            fields, files = {}, {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                data = part.get_payload(decode=True)
                (files if part.get_filename() else fields)[name] = (
                    data if part.get_filename() else data.decode("utf-8"))
            body, payload = fields, manifest(fields, files)
            seen.update(fields=fields, files={k: len(v) for k, v in files.items()})
        else:
            body, payload = json.loads(raw), raw
            seen["body"] = body
            seen["first_key"] = next(iter(body), None)
        if body.get("deviceId") != DEVICE:
            return 401, None, {"code": 0, "data": None, "message": "Device not registered"}
        if not ceravis_api._signed_by(DEVICE_KEY, ts, payload, sig):
            return 401, None, {"code": 0, "data": None, "message": "Invalid signature"}
        if sig in self.used:
            return 401, None, {"code": 0, "data": None, "message": "Request already used"}
        self.used.add(sig)
        patient = body.get("patientUserId") or body.get("userId") or body.get("patientId")
        if path != "recordings/event" and str(patient) != str(PATIENT):
            return 403, DEVICE_KEY, {"code": 0, "data": None, "message": "Not allowed for this device"}
        data: object = None
        if path == "saveAlert":
            data = {"alertId": self.alerts.setdefault(seen["idem"], 9000 + len(self.alerts))}
        elif path == "getPatientPostures":
            data = [{"postureType": "STANDING", "frontView": {
                "postureImageId": 77, "downloadUrl": "https://ceravis-media.s3.amazonaws.com/p/77.jpg"}}]
        elif path == "deletePatientZoningFile" and not self.zoning_stored:
            return 404, None, {"code": 0, "data": None, "message": "No zoning file"}
        return 200, DEVICE_KEY, {"code": 1, "data": data, "message": "OK"}


APP = AppServer()


class _Handler(BaseHTTPRequestHandler):
    def _any(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path.split("/v1/ai/", 1)[-1]
        status, key, reply = APP.handle(self.command, path, self.headers, raw)
        out = json.dumps(reply).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if key and not APP.unsigned:
            self.send_header(ceravis_api.HDR_SIGNATURE, ceravis_api._signature(
                key, self.headers.get(ceravis_api.HDR_TIMESTAMP), out))
        if APP.tamper:
            out = out.replace(b'"OK"', b'"OK!"')
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_PUT = do_POST = _any

    def date_time_string(self, timestamp=None):      # the server's own clock, for Date
        return super().date_time_string(time.time() + APP.skew)

    def log_message(self, *_):
        pass


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
settings.ceravis_api_base_url = f"http://127.0.0.1:{SERVER.server_address[1]}/ch"
settings.ceravis_api_key = "test-api-key-not-a-real-one"
KEY_FILE = _TMP / ceravis_api.EDGE_KEY_FILE


def raises(fn) -> CeravisApiError | None:
    try:
        fn()
    except CeravisApiError as exc:
        return exc
    return None


# --------------------------------------------------------------------------
print("\n1. before its first sign-in a device sends nothing")
exc = raises(lambda: ceravis_api.save_alert(PATIENT, "FALL", "Fall detected"))
check("no device key: refused on the device (401, so the outbox keeps it and flags it), "
      "nothing sent", exc is not None and exc.status == 401 and "sign in" in str(exc)
      and not APP.seen, str(exc))
KEY_FILE.write_text(DEVICE_KEY, encoding="utf-8")

# --------------------------------------------------------------------------
print("\n2. all eight calls: deviceId first, no API key, signed with the device key")
CAM = {"device": "CAM_01", "model": "C220", "supplier": "SN1", "room": "LIVING_ROOM",
       "ptzSupported": True, "isEnabled": True}
SEGMENT = {"edgeId": "ce7k2m9xq4w8n3", "camera_id": "LIVING_ROOM", "status": "started",
           "segment": {"start": "2026-11-20T10:00:00+05:30", "end": None, "seconds": None}}
results = {
    "saveCamera": ceravis_api.save_cameras(PATIENT, [CAM]),
    "cameras/delete": ceravis_api.delete_camera(PATIENT, "LIVING_ROOM"),
    "deletePatientZoningFile": ceravis_api.delete_patient_zoning_file(PATIENT),
    "uploadEmbeddingFile": ceravis_api.upload_embedding_file("ZONING", PATIENT, "zones.json",
                                                              b'{"LIVING_ROOM": []}'),
    "getPatientPostures": ceravis_api.get_patient_postures(PATIENT),
    "saveAlert": ceravis_api.save_alert(PATIENT, "FALL", "Fall detected in LIVING_ROOM",
                                        idempotency_key="job-1"),
    "recordings/event": ceravis_api.send_recording_event(SEGMENT),
    "saveSnapshot": ceravis_api.save_snapshot(PATIENT, "Fall detected in LIVING_ROOM", "CAM_01",
                                              image=b"\xff\xd8jpeg", alert_id=9000,
                                              category="FALL", idempotency_key="job-2"),
}
json_calls = [s for s in APP.seen if "body" in s]
check("eight calls reached the server, each accepted",
      [s["path"] for s in APP.seen] == list(results), str([s["path"] for s in APP.seen]))
check("...every JSON body names the device FIRST",
      len(json_calls) == 7 and all(s["first_key"] == "deviceId" and s["body"]["deviceId"] == DEVICE
                                   for s in json_calls))
check("...the multipart upload names it in a deviceId field",
      APP.seen[-1]["fields"].get("deviceId") == DEVICE)
check("...no call carries an API key", not any(s["api_key"] for s in APP.seen))
check("...answers as before: saveAlert's alertId, the posture list, a 404 zoning delete = done",
      ceravis_api.alert_id_of(results["saveAlert"]) == 9000
      and results["getPatientPostures"][0]["postureType"] == "STANDING"
      and results["deletePatientZoningFile"] is True and results["recordings/event"] is None)
check("...the bodies are the ones the server expects, values unchanged",
      APP.seen[0]["body"] == {"deviceId": DEVICE, "patientUserId": PATIENT, "cameras": [CAM]}
      and APP.seen[1]["body"] == {"deviceId": DEVICE, "userId": PATIENT, "room": "LIVING_ROOM"}
      and APP.seen[6]["body"] == {"deviceId": DEVICE, **SEGMENT})

# --------------------------------------------------------------------------
print("\n3. saveSnapshot is signed over its manifest: fields and files, any mix")
for label, kw in (
        ("a still with an alert link", dict(image=b"\xff\xd8a", alert_id=9001, category="FALL")),
        ("a clip and a still, no category", dict(image=b"\xff\xd8b", video=b"\x00\x00mp4")),
        ("a clip alone, empty text", dict(video=b"\x00\x00mp4c", text="")),
        ("non-ASCII text", dict(image=b"\xff\xd8d", text="Sturz erkannt — Küche"))):
    text = kw.pop("text", "Snapshot")
    ok = raises(lambda: ceravis_api.save_snapshot(PATIENT, text, "CAM_02", **kw)) is None
    check(f"{label}: accepted", ok and APP.seen[-1]["path"] == "saveSnapshot", str(APP.seen[-1])[:160])
check("the edge's manifest is the rule's, line for line",
      ceravis_api._snapshot_manifest({"deviceId": "d", "patientId": "5", "text": "",
                                      "cameraNumber": "C"}, {"video": ("v.mp4", b"v", "video/mp4")})
      == manifest({"deviceId": "d", "patientId": "5", "text": "", "cameraNumber": "C"}, {"video": b"v"}))
exc = raises(lambda: ceravis_api.save_snapshot(PATIENT, "x", "CAM_01"))
check("nothing to send: refused on the device", exc is not None and "nothing to send" in str(exc))

# --------------------------------------------------------------------------
print("\n4. one Idempotency-Key across retries, each retry signed anew")
first = ceravis_api.save_alert(PATIENT, "NO_MOTION", "No motion", idempotency_key="job-7")
again = ceravis_api.save_alert(PATIENT, "NO_MOTION", "No motion", idempotency_key="job-7")
a, b = APP.seen[-2], APP.seen[-1]
check("the same key on both tries, two different signatures, the same alert",
      a["idem"] == b["idem"] == "job-7" and a["sig"] != b["sig"]
      and ceravis_api.alert_id_of(first) == ceravis_api.alert_id_of(again))

# --------------------------------------------------------------------------
print("\n5. a reply never taken as delivered unless it is the server's")
APP.tamper = True
exc = raises(lambda: ceravis_api.save_alert(PATIENT, "FALL", "x", idempotency_key="job-8"))
APP.tamper = False
check("altered on the way: refused as unverified (the outbox keeps it and retries)",
      exc is not None and str(exc) == ceravis_api._UNVERIFIED and exc.status == 200, str(exc))
APP.unsigned = True
exc = raises(lambda: ceravis_api.save_cameras(PATIENT, [CAM]))
APP.unsigned = False
check("unsigned: refused as unverified", exc is not None and str(exc) == ceravis_api._UNVERIFIED)
exc = raises(lambda: ceravis_api.save_alert(4000, "FALL", "x"))
check("another patient's alert: the server's 403 is an error with its message",
      exc is not None and exc.status == 403 and "Not allowed for this device" in str(exc), str(exc))

# --------------------------------------------------------------------------
print("\n6. the clock")
APP.skew = 60.0                                   # the server's clock is a minute ahead
before = len(APP.seen)
ok = raises(lambda: ceravis_api.save_cameras(PATIENT, [CAM])) is None
check("a device clock 60 s behind: refused once, corrected from the Date header, accepted",
      ok and len(APP.seen) == before + 2 and 55 <= ceravis_api._clock_offset <= 65,
      f"offset={ceravis_api._clock_offset:.1f} calls={len(APP.seen) - before}")
before = len(APP.seen)
ok = raises(lambda: ceravis_api.delete_camera(PATIENT, "KITCHEN")) is None
check("...the next call goes straight through on the corrected time",
      ok and len(APP.seen) == before + 1)
APP.skew = 0.0                                    # the device's NTP catches up
ok = raises(lambda: ceravis_api.delete_camera(PATIENT, "KITCHEN")) is None
check("...and when the clocks agree again, the correction is dropped",
      ok and ceravis_api._clock_offset == 0.0, f"offset={ceravis_api._clock_offset}")
APP.skew = 1.0
ceravis_api.delete_camera(PATIENT, "KITCHEN")
check("a gap of a second (the Date header's own resolution) is left alone",
      ceravis_api._clock_offset == 0.0, f"offset={ceravis_api._clock_offset}")
APP.skew = 0.0

# --------------------------------------------------------------------------
print("\n7. the wire log")
wire = (_TMP / ceravis_api._WIRE_FILE).read_text(encoding="utf-8")
records = [json.loads(line) for line in wire.splitlines()]
snap = next(r for r in records if r["endpoint"] == "saveSnapshot")
check("the request as sent, deviceId in it; file bytes only as size markers",
      snap["request"]["deviceId"] == DEVICE and snap["request"]["image"].startswith("<image:")
      and next(r for r in records if r["endpoint"] == "uploadEmbeddingFile")["request"]
      ["fileBytes"].startswith("<fileBytes:"))
check("no key and no API key anywhere in it",
      DEVICE_KEY not in wire and settings.ceravis_api_key not in wire)

# --------------------------------------------------------------------------
print("\n8. posture images come only from Amazon S3 over https")
from api.recipient_routes import _cloud_storage_link               # noqa: E402
for url, want in (("https://ceravis-media.s3.amazonaws.com/p/77.jpg?X-Amz-Signature=1", True),
                  ("https://s3.ap-south-1.amazonaws.com/ceravis/p.jpg", True),
                  ("http://ceravis-media.s3.amazonaws.com/p.jpg", False),
                  ("https://evil.example.com/amazonaws.com/p.jpg", False),
                  ("https://amazonaws.com.evil.example/p.jpg", False),
                  ("https://169.254.169.254/latest/meta-data", False),
                  ("file:///etc/passwd", False), ("", False)):
    check(f"{url or '(empty)'} -> {'fetched' if want else 'refused'}", _cloud_storage_link(url) is want)

SERVER.shutdown()
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All signed-call checks passed.")
