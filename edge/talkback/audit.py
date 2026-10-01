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
_BYTES_PER_SEC = 8000
# A pause longer than this between frames is the button coming up, not jitter.
_SPAN_GAP = 0.5
# Below this much sending time the rate is not judged: a short press is mostly
# start-up burst.
_JUDGE_SECS = 2.0


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
        self._last = None
        self._wall = 0.0                            # seconds spent sending
        self._spans = 0                             # separate presses seen
        self._close: dict | None = None

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
        elif message.get("type") == "error":
            self.entry["error"] = message

    def frame(self, size: int, speech: bool) -> None:
        now = time.monotonic()
        if self._last is None or now - self._last > _SPAN_GAP:
            self._spans += 1
        else:
            self._wall += now - self._last
        self._last = now
        self.frames += 1
        self.bytes += size
        self.speech += speech
        self.sizes[size] = self.sizes.get(size, 0) + 1

    def finish(self) -> None:
        audio_secs = self.bytes / _BYTES_PER_SEC
        # Each press contributes one frame's worth of time the gaps cannot see.
        sending = self._wall + self._spans * FRAME_BYTES / _BYTES_PER_SEC
        rate = round(audio_secs / sending, 2) if sending >= _JUDGE_SECS else None
        self.entry.update({
            "ended_at": now_iso(),
            "duration_secs": round(time.monotonic() - self._t0, 1),
            "close": self._close or {"code": None, "by": "client", "reason": ""},
            "audio": {"frames": self.frames, "bytes": self.bytes,
                      "frame_sizes": {str(k): v for k, v in sorted(self.sizes.items())},
                      "speech_secs": round(self.speech * FRAME_BYTES / _BYTES_PER_SEC, 1),
                      "sending_secs": round(sending, 1) if self.frames else 0.0,
                      "realtime_rate": rate},
        })
        warnings = self._judge(rate)
        if warnings:
            self.entry["warnings"] = warnings
        record(self.entry)

    def _judge(self, rate: float | None) -> list[str]:
        """What a client developer must fix, in their words, from the audio
        that actually arrived."""
        granted = (self.entry["response"] or {}).get("type") == "open"
        out = []
        if granted and not self.frames:
            out.append("Granted, but no audio arrived — the app never sent frames "
                       "(binary 160-byte A-law, after `open`).")
            return out
        odd = sorted(s for s in self.sizes if s != FRAME_BYTES)
        if odd:
            out.append(f"Frames of {', '.join(map(str, odd))} bytes arrived — send "
                       f"exactly {FRAME_BYTES}-byte frames (20 ms of 8 kHz A-law).")
        if self.frames and not self.speech:
            out.append("Only silence arrived — the microphone is muted or not "
                       "capturing (every byte was A-law zero).")
        if rate is not None and rate > 1.3:
            out.append(f"Audio arrived at {rate}x real time — more samples than 8 kHz "
                       f"are being sent: the app is not resampling its microphone "
                       f"to 8000 Hz, so the room hears it slow and deep.")
        elif rate is not None and rate < 0.75:
            out.append(f"Audio arrived at {rate}x real time — about "
                       f"{round((1 - rate) * 100)}% of the speech is missing (frames "
                       f"dropped before sending, or sent too slowly), so the room "
                       f"hears it broken. Send EVERY 160-byte frame the microphone "
                       f"produces, not one per callback.")
        return out
