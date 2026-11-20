#!/usr/bin/env python3
"""
Removing the care recipient from the device (maintenance/recipient_reset.py).

Covered:
  every place the recipient's data lives is its OWNER's own place (no drift:
  enrollment, events DB, snapshots, outbox, recordings, proofs — and the
  call/wire/talk logs, rotated copies included, which are set apart);
  nothing is erased by asking;
  request() only leaves the marker; the erasure runs at service start:
    the logs move under logs/earlier_recipients/<id>_<when>/ with whose they
    were, every other part erased, the marker dropped, a second start does
    nothing; the device's own files stay byte-identical (cameras, zones,
    talk-back password, hotspot, local key, app-server key, reboot password,
    recording mode);
  sign-ins: after a removal every browser is signed out; after a hand-over the
    NEW recipient's own sign-ins stay, the previous ones end;
  the routes: remove-recipient needs the code AND the app server's unassign;
  then it clears the account at once, marks the rest, queues the restart;
  wiring: main.py finishes a removal before the pipeline opens a file; the
  Setup page asks for the code in its header's Remove button.

    python tests/test_recipient_reset.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

_TMP = Path(tempfile.mkdtemp(prefix="ceravis-reset-"))
os.environ["DATA_DIR"] = str(_TMP)

from fastapi import BackgroundTasks                                # noqa: E402

from config.settings import settings                               # noqa: E402
from maintenance import recipient_reset as rr                      # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


D = Path(settings.data_dir)


def put(rel: str, data: bytes = b"x" * 10) -> Path:
    p = D / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


print("\nevery place is its owner's own")
from enrollment.enrollment_manager import EnrollmentManager       # noqa: E402
from integration import call_log, ceravis_api                     # noqa: E402
from talkback import audit                                        # noqa: E402
from common.event_snapshots import events_root                    # noqa: E402
from recording.index import record_root                           # noqa: E402
from recording.proof import _proof_root                           # noqa: E402
from storage.outbox_store import OutboxStore                      # noqa: E402

put(f"{ceravis_api._WIRE_FILE}")
put("cloud_calls.jsonl")
put("talkback_log.jsonl")
put("talkback_log.jsonl.1")
places = {p for part in rr.parts() for p in part.paths} | set(rr.log_files())
owners = {
    "enrollment": EnrollmentManager().base_path,
    "events db": Path(settings.sqlite_path),
    "snapshots": events_root(),
    "outbox spool": OutboxStore._spool_dir(),
    "recordings": record_root(),
    "proof stills": _proof_root(),
    "call log": call_log._file(),
    "wire log": D / ceravis_api._WIRE_FILE,
    "talk-back log": audit.PATH,
    "talk-back log (rotated)": audit.PATH.with_suffix(".jsonl.1"),
}
missing = [k for k, p in owners.items() if p.resolve() not in {x.resolve() for x in places}]
check("each owner's place is erased", not missing, str(missing))

print("\na device in use")
KEEP = {
    "cameras.json": b'[{"camera_id": "LIVING_ROOM"}]',
    "zones.json": b'{"LIVING_ROOM": []}',
    "talkback.json": b'{"home": "hash"}',
    "hotspot.json": b'{"ssid": "CERAVIS-X"}',
    "local.key": b"k" * 43,
    "edge_api.key": b"3c9f" * 16,
    "reboot_auth.json": b'{"hash": "h"}',
    "recording_mode.json": b'{"mode": "on"}',
}
for rel, data in KEEP.items():
    put(rel, data)
put("account.json", json.dumps({"ceravisUserId": 5012, "email": "old@example.com",
                                "firstName": "Arnold"}).encode())
put("recipients/5012/photos/1.jpg", b"j" * 1000)
put("recipients/5012/body/embeddings.npy", b"n" * 2000)
put("ceravis.db", b"d" * 4096)
put("ceravis.db-wal", b"w" * 100)
put("events/edge/2026-11-12/e1.jpg", b"e" * 500)
put("outbox/blob1", b"o" * 300)
put("recordings/LIVING_ROOM/2026-11-12_10-00-00.mp4", b"v" * 9000)
put("recordings_proof/p.jpg", b"p" * 200)

sizes = {part.key: part.size() for part in rr.parts()}
check("each part knows what it holds; the logs are not among the parts erased",
      sizes["enrollment"] == (2, 3000) and sizes["recordings"][0] == 2
      and sizes["events"][0] >= 4 and "logs" not in sizes and len(rr.log_files()) == 4,
      str(sizes))
check("asking erases nothing", (D / "recipients/5012/photos/1.jpg").exists())
check("nothing pending at first; a start with nothing pending does nothing",
      rr.pending() is None and rr.complete_pending() is None)

print("\nremove the recipient: the route")
from api import account_routes                                    # noqa: E402
from api.auth import sessions                                     # noqa: E402
from starlette.requests import Request                            # noqa: E402

req = Request({"type": "http", "client": ("10.0.0.5", 1), "headers": []})
asked: list = []
account_routes.verify_otp = lambda email, otp: (asked.append(("verifyOtp", email)) or
                                                ((True, None, {}) if otp == "24680"
                                                 else (False, "Invalid OTP", None)))
account_routes.unassign_device = lambda email: asked.append(("unassignDevice", email)) or (True, None)
tasks = BackgroundTasks()
r = account_routes.remove_recipient(account_routes.RemoveRecipientRequest(), req, tasks)
check("without a code nothing happens, nothing is asked",
      r["removed"] is False and (D / "account.json").exists() and rr.pending() is None
      and not tasks.tasks and not asked)
r = account_routes.remove_recipient(account_routes.RemoveRecipientRequest(otp="13579"), req, tasks)
check("a wrong code: nothing happens, the app server is not asked to unassign",
      r["removed"] is False and asked == [("verifyOtp", "old@example.com")]
      and (D / "account.json").exists() and rr.pending() is None and not tasks.tasks)
old_a, old_b = sessions.issue("old@example.com"), sessions.issue("carer@example.com")
r = account_routes.remove_recipient(account_routes.RemoveRecipientRequest(otp="24680"),
                                    req, tasks)
check("the code, then the app server's unassign: the account goes at once, the rest is "
      "marked (with whose it was), the restart queued",
      r["removed"] is True and r["restarting"] is True
      and asked[-2:] == [("verifyOtp", "old@example.com"), ("unassignDevice", "old@example.com")]
      and rr.pending()["earlier"]["ceravisUserId"] == 5012
      and not (D / "account.json").exists() and rr.pending() is not None
      and [t.func.__name__ for t in tasks.tasks] == ["restart_soon"])
check("...but nothing else is erased under the running service",
      (D / "recipients/5012/photos/1.jpg").exists() and (D / "ceravis.db").exists())

print("\nthe service starts again")
folder = D / "logs" / rr.EARLIER / rr.pending()["folder"]
done = rr.complete_pending()
left = [p for part in rr.parts() for p in part.paths if p.exists()]
check("every part erased", done is not None and not left, f"{done} left={left}")
check("the logs are set apart under the earlier recipient, whose they were noted",
      sorted(f.name for f in folder.iterdir()) == sorted(
          ["recipient.json", "cloud_calls.jsonl", ceravis_api._WIRE_FILE,
           "talkback_log.jsonl", "talkback_log.jsonl.1"])
      and json.loads((folder / "recipient.json").read_text())["ceravisUserId"] == 5012
      and json.loads((folder / "recipient.json").read_text())["email"] == "old@example.com",
      str(sorted(f.name for f in folder.iterdir())) if folder.exists() else "no folder")
check("...and the new call log holds only the removal's own note",
      len(call_log._file().read_text(encoding="utf-8").splitlines()) == 1)
check("the device's own files are byte-identical",
      all((D / rel).read_bytes() == data for rel, data in KEEP.items()))
check("the marker is gone; the next start does nothing",
      rr.pending() is None and rr.complete_pending() is None)
check("every browser is signed out (no account on the device)",
      sessions.check(old_a) is None and sessions.check(old_b) is None)
check("the removal is noted on the sync console",
      "Care recipient data removed" in call_log._file().read_text(encoding="utf-8"))

print("\na hand-over keeps only the NEW recipient's sign-ins")
put("recipients/5012/photos/2.jpg")
put("account.json", json.dumps({"ceravisUserId": 6001, "email": "New@Example.com"}).encode())
rr.request("handed over to another care recipient", "new@example.com", {"ceravisUserId": 5012})
new_s, old_s = sessions.issue("new@example.com"), sessions.issue("old@example.com")
rr.complete_pending()
check("the previous recipient's data is erased, the new account stays",
      not (D / "recipients").exists()
      and json.loads((D / "account.json").read_text())["ceravisUserId"] == 6001)
check("the new recipient stays signed in (email case aside); the old one is out",
      sessions.check(new_s) is not None and sessions.check(old_s) is None)

print("\na marker that cannot be read is still finished")
put("recipients/1/photos/a.jpg")
put(rr.MARKER, b"{not json")
check("pending, and erased on start",
      rr.pending() is not None and rr.complete_pending() is not None
      and not (D / "recipients").exists() and rr.pending() is None)

print("\nwiring")
main = (ROOT / "edge/main.py").read_text(encoding="utf-8")
life = main[main.index("async def lifespan"):]
check("main.py finishes a removal before the pipeline opens any file",
      "recipient_reset.complete_pending()" in life
      and life.index("complete_pending") < life.index("Pipeline()"))
setup = (ROOT / "edge/static/setup.html").read_text(encoding="utf-8")
check("the Setup page's header button asks for the emailed code, then waits out the restart",
      'class="ph-actions"' in setup and "/api/v1/account/remove-recipient/code" in setup
      and "cvOtpBoxes(" in setup and "cvWaitForRestart(" in setup
      and "recipient-data" not in setup and 'typeToConfirm: "REMOVE"' not in setup)
shared = (ROOT / "edge/static/ceravis.js").read_text(encoding="utf-8")
check("the confirm dialog builds its lists as text, never HTML",
      "li.textContent = t" in shared and "head.textContent = sec.title" in shared)

shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All care recipient removal checks passed.")
