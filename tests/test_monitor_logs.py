"""
The monitor's logs: GET /api/v1/logs/{name} and the reader behind it.

1. Logbook.tail reads a log from its END: the right lines, oldest first, across
   rotated files, across 64 KB block edges, never a half line
2. the endpoint: newest first; only the five allowed logs; `since` answers
   "unchanged" without reading; a line cut mid-write is skipped; limit capped;
   the video server's log comes as text lines
3. who may read it: the bench consoles (UI_TESTING_OPEN) open exactly these
   five names — nothing else
4. the page: a card for every category, each fed by its endpoint

    python tests/test_monitor_logs.py
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
os.chdir(ROOT / "edge")
DATA = Path(tempfile.mkdtemp(prefix="ceravis-monlogs-"))

failures: list[str] = []


def check(label: str, ok: bool, detail="") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + str(detail)[:300]) if detail else ''}")
    if not ok:
        failures.append(label)


from config.settings import settings                       # noqa: E402

settings.data_dir = str(DATA)
from common.logbook import Logbook, log_dir                 # noqa: E402

print("\n1. Logbook.tail reads from the end")
book = Logbook(DATA / "t" / "big.jsonl", max_bytes=10 ** 9, backups=2)
pad = "x" * 900                       # ~1 KB lines: 300 of them cross several 64 KB blocks
for i in range(300):
    book.write({"n": i, "pad": pad})
last = [json.loads(r)["n"] for r in book.tail(150)]
check("the newest 150 of 300 one-kilobyte lines, oldest first, none cut",
      last == list(range(150, 300)), last[:3])
check("asking for more than there is returns all of it", len(book.tail(1000)) == 300)
check("tail(1) is the newest line only", json.loads(book.tail(1)[0])["n"] == 299)
small = Logbook(DATA / "t" / "rot.jsonl", max_bytes=300, backups=2)
for i in range(30):
    small.write({"n": i, "pad": "y" * 40})
check("across rotated files (.1, .2) in order",
      [json.loads(r)["n"] for r in small.tail(6)] == list(range(24, 30)))
v1 = small.version()
small.write({"n": 30})
check("version() changes when the log is written", v1 and small.version() and v1 != small.version())
check("a log that does not exist: no lines, no version",
      Logbook(DATA / "none.jsonl", 1, 1).tail(5) == [] and Logbook(DATA / "none.jsonl", 1, 1).version() is None)

print("\n2. GET /api/v1/logs/{name}")
from fastapi import HTTPException                           # noqa: E402

import api.log_routes as lr                                 # noqa: E402

events = Logbook(log_dir(DATA) / "events.jsonl", max_bytes=10 ** 6, backups=1)
for i in range(5):
    events.write({"event": "warning_raised", "part": "hardware", "warning": f"w{i}"})
with open(events.path, "a", encoding="utf-8") as fh:
    fh.write('{"event": "cut mid-wri')              # a crash mid-write
r = lr.read_log("events", limit=10)
check("newest first; the cut line is skipped",
      [e["warning"] for e in r["entries"]] == ["w4", "w3", "w2", "w1", "w0"], r["entries"][:2])
same = lr.read_log("events", limit=10, since=r["version"])
check("`since` = the last version: answered `unchanged`, nothing read",
      same == {"name": "events", "version": r["version"], "unchanged": True}, same)
events = Logbook(events.path, max_bytes=10 ** 6, backups=1)       # the app started again
events.write({"event": "warning_cleared", "part": "hardware", "warning": "w4"})
check("...and once written again (after a restart), the new entry is there — not glued to the cut line",
      lr.read_log("events", limit=1, since=r["version"])["entries"][0]["event"] == "warning_cleared")
check("limit is capped (300) and at least 1",
      len(lr.read_log("events", limit=10 ** 6)["entries"]) == 6
      and len(lr.read_log("events", limit=0)["entries"]) == 1)
(DATA / "mediamtx.log").write_text("INF one\nERR two\n", encoding="utf-8")
check("the video server's log as text lines, newest first",
      lr.read_log("mediamtx")["entries"] == [{"line": "ERR two"}, {"line": "INF one"}])
check("a log not written yet: empty, no version",
      lr.read_log("fleet") == {"name": "fleet", "version": None, "entries": []})
refused = []
for name in ("../../etc/passwd", "ceravis_api_wire", "talkback_log", "cloud_calls"):
    try:
        lr.read_log(name)
    except HTTPException as exc:
        refused.append(exc.status_code)
check("any other name — a path, the wire or talk-back log — is a 404", refused == [404] * 4, refused)

print("\n3. who may read it")
from api import auth                                        # noqa: E402

opened = [n for n in lr._FILES if auth._matches(auth._TESTING_RE, "GET", f"/api/v1/logs/{n}")]
check("the bench consoles may read exactly the five logs",
      opened == list(lr._FILES) and not auth._matches(auth._TESTING_RE, "GET", "/api/v1/logs/secrets")
      and not auth._matches(auth._TESTING_RE, "POST", "/api/v1/logs/events"), opened)
check("...and never without UI_TESTING_OPEN: not public, not a cloud route",
      not auth._matches(auth._PUBLIC_RE, "GET", "/api/v1/logs/events")
      and not auth._matches(auth._CLOUD_RE, "GET", "/api/v1/logs/events"))

print("\n4. the monitor page")
page = (ROOT / "edge/static/ui-testing/monitor.html").read_text(encoding="utf-8")
cats = ["fleet", "out", "in", "incoming", "talk", "events", "video"]
check("a card, a dot, counters and a list for every category",
      all(f'data-cat="{c}"' in page and f'id="{c}-dot"' in page and f'id="{c}-stats"' in page
          and f'id="{c}-log"' in page for c in cats))
check("each fed by its source",
      all(s in page for s in ('deviceLog("fleet"', 'deviceLog("events"', 'deviceLog("incoming"',
                              'deviceLog("mediamtx"', 'deviceLog("health"', "/api/v1/cloud/activity",
                              "/api/v1/talkback/log")))
check("every request and response opens from its entry", page.count("ioPanel(e.req, e.res") == 1)

shutil.rmtree(DATA, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All monitor-log checks passed.")
