from __future__ import annotations

"""
The talk log — who spoke into which room, when, for how long, and how well.

One JSON line per event in `data/talkback_log.jsonl`:

    talk     a carer held a camera's floor: started_at, ended_at, talk_secs
             (seconds of actual speech), held_secs, and how it ended
    refused  a carer tried and was refused, and why (busy, password, offline)
    session  one talk WebSocket, as a CALL and its RESPONSE: what the client
             asked for, what the edge answered (`open`, or the refusal), how
             it closed — and what audio actually arrived, judged against the
             contract (160-byte A-law frames at real time, 8 kHz). That last
             part is what tells an app team "your audio is half missing" or
             "you are not resampling to 8 kHz" without a microphone in the room.

The carer's `name` and `user_id` are what their app sent on the socket. The edge
cannot verify them (the edge_id is the only credential it checks), so the log is
as trustworthy as the app that connects — the same as every other field a client
supplies. The size is capped: past _MAX_BYTES the file rotates to `.1`, so the
last two files are always there and the disk never fills.

Writing never raises: a full disk must not cost a carer their conversation.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from common.clock import now_iso

logger = logging.getLogger("talkback.audit")

_DATA = Path(__file__).resolve().parents[1] / "data"
PATH = _DATA / "talkback_log.jsonl"
_MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()

# The audio contract a client must meet (talkback_routes: the `open` message).
FRAME_BYTES = 160                # 20 ms of 8 kHz G.711 A-law
RATE_HZ = 8000                   # one A-law byte is one sample
# Between two pieces, more than this is a GAP in the audio, not network jitter.
_GAP = 0.2
# Under this much button-held time the rate is not judged (start-up burst).
_JUDGE_SECS = 1.0


def record(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            if PATH.exists() and PATH.stat().st_size >= _MAX_BYTES:
                os.replace(PATH, PATH.with_suffix(".jsonl.1"))
            with open(PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        logger.warning("talk log not written", exc_info=True)


def recent(camera_id: str | None = None, limit: int = 100,
           events: set[str] | None = None) -> list[dict]:
    """The newest `limit` entries, newest first, optionally for one camera and
    only of the given event kinds."""
    out: list[dict] = []
    for path in (PATH, PATH.with_suffix(".jsonl.1")):
        try:
            rows = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in reversed(rows):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if camera_id is not None and row.get("camera_id") != camera_id:
                continue
            if events is not None and row.get("event") not in events:
                continue
            out.append(row)
            if len(out) >= limit:
                return out
    return out


class Session:
    """One talk socket, written as ONE `session` entry when it ends.

    Cheap on the audio path: a frame costs a few additions. Everything that
    needs judging is done once, in finish()."""

    def __init__(self, camera_id: str, query: dict, *, holder: str, user_agent: str,
                 name: str, user_id: str, client_id: str) -> None:
        q = dict(query)
        for key in ("edge_id", "edgeId"):
            if q.get(key):
                q[key] = "…" + q[key][-6:]          # readable, and not the whole key
        self._t0 = time.monotonic()
        self.entry = {
            "event": "session", "camera_id": camera_id,
            "name": name, "user_id": user_id, "client_id": client_id,
            "holder": holder, "started_at": now_iso(),
            "request": {"path": f"/api/v1/talkback/{camera_id}/stream", "query": q,
                        "user_agent": (user_agent or "")[:160]},
            "response": None,
        }
        self.frames = self.bytes = self.speech = 0
        self.sizes: dict[int, int] = {}
        self._close: dict | None = None
        # The button, as the audio shows it: a press starts with its first
        # piece and ends at the client's `release` (or its last piece).
        self._opened_at: float | None = None
        self._first_audio_ms: int | None = None
        self._press_start: float | None = None
        self._last: float | None = None
        self._held = 0.0
        self.releases = 0
        self.gaps = 0
        self._longest_gap = 0.0
        self._win_t = self._win_bytes = 0.0       # the live window for `stats`

    def closed(self, code: int | None, by: str, reason: str = "") -> None:
        """How the socket ended — the FIRST account wins (a refusal's code, not
        the tidy-up close that follows it)."""
        if self._close is None:
            self._close = {"code": code, "by": by, "reason": reason[:120]}

    def answered(self, message: dict) -> None:
        """The edge's answer to the call: `open`, or the refusal. Later
        refusals (someone took the room) are kept as `error`."""
        if self.entry["response"] is None:
            self.entry["response"] = message
            if message.get("type") == "open":
                self._opened_at = time.monotonic()
        elif message.get("type") == "error":
            self.entry["error"] = message

    def frame(self, size: int, speech: bool) -> None:
        now = time.monotonic()
        if self._press_start is None:                 # a press begins
            self._press_start = now
            if self._first_audio_ms is None and self._opened_at is not None:
                self._first_audio_ms = round((now - self._opened_at) * 1000)
        else:                                         # inside a press: was there a hole?
            gap = now - self._last
            self.gaps += gap > _GAP
            self._longest_gap = max(self._longest_gap, gap)
        self._last = now
        self.frames += 1
        self.bytes += size
        self.speech += speech
        self.sizes[size] = self.sizes.get(size, 0) + 1
        self._win_bytes += size

    def released(self) -> None:
        """The client said the button came up: the press ends here."""
        self.releases += 1
        if self._press_start is not None:
            self._held += time.monotonic() - self._press_start
            self._press_start = None

    def window_hz(self) -> int | None:
        """Samples received per second since the last call — the live figure
        the client's `stats` message carries."""
        now = time.monotonic()
        if not self._win_t:
            self._win_t, self._win_bytes = now, 0.0
            return None
        hz = round(self._win_bytes / max(now - self._win_t, 1e-3))
        self._win_t, self._win_bytes = now, 0.0
        return hz

    def finish(self) -> None:
        if self._press_start is not None:             # a press that never sent release
            self._held += self._last - self._press_start + FRAME_BYTES / RATE_HZ
        held = self._held
        received_hz = round(self.bytes / held) if held >= _JUDGE_SECS else None
        rate = round(received_hz / RATE_HZ, 2) if received_hz is not None else None
        self.entry.update({
            "ended_at": now_iso(),
            "duration_secs": round(time.monotonic() - self._t0, 1),
            "close": self._close or {"code": None, "by": "client", "reason": ""},
            "audio": {"frames": self.frames, "bytes": self.bytes,
                      "frame_sizes": {str(k): v for k, v in sorted(self.sizes.items())},
                      "speech_secs": round(self.speech * FRAME_BYTES / RATE_HZ, 1),
                      "held_secs": round(held, 1),
                      "expected_hz": RATE_HZ, "received_hz": received_hz,
                      "realtime_rate": rate,
                      "gaps": self.gaps,
                      "longest_gap_ms": round(self._longest_gap * 1000),
                      "first_audio_ms": self._first_audio_ms,
                      "releases": self.releases},
        })
        warnings = self._judge(rate, received_hz)
        if warnings:
            self.entry["warnings"] = warnings
        record(self.entry)

    def _judge(self, rate: float | None, hz: int | None) -> list[str]:
        """What a client developer must fix, in their words, from the audio
        that actually arrived."""
        granted = (self.entry["response"] or {}).get("type") == "open"
        out = []
        if granted and not self.frames:
            out.append("Granted, but no audio arrived — the app never sent pieces "
                       "(binary 160-byte A-law, after `open`).")
            return out
        odd = sorted(s for s in self.sizes if s != FRAME_BYTES)
        if odd:
            out.append(f"Pieces of {', '.join(map(str, odd))} bytes arrived — send "
                       f"exactly {FRAME_BYTES}-byte pieces (20 ms of 8 kHz A-law).")
        if self.frames and not self.speech:
            out.append("Only silence arrived — the microphone is muted or not "
                       "capturing (every byte was A-law zero).")
        if rate is not None and rate > 1.3:
            out.append(f"Received {hz} Hz, needs {RATE_HZ} Hz ({rate}x) — the app is "
                       f"not resampling its microphone to 8000 Hz, so the room hears "
                       f"it slow and deep.")
        elif rate is not None and rate < 0.8:
            gap = round(self._longest_gap * 1000)
            out.append(f"Received {hz} Hz, needs {RATE_HZ} Hz — only {round(rate * 100)}% "
                       f"of the audio arrived (longest silence between pieces: {gap} ms), "
                       f"so the room hears it broken. Send every 160-byte piece the "
                       f"moment the microphone produces it: no timers, no waiting, "
                       f"no logging per piece.")
            if not self.releases and gap > 1000:
                out.append("No `release` was sent — if the button went up and down "
                           "inside this socket, send {\"type\":\"release\"} on button up "
                           "so a pause is not counted as missing audio.")
        return out
