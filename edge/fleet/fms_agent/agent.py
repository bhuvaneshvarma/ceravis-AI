"""The loop: (enroll once) → gather → exchange → act → sleep, forever.

Rules it lives by:
  • No per-device setup. With no identity yet it ENROLLS by itself: it waits
    briefly for the edge app so an existing edge_id is adopted, then gets its
    permanent edge_id and its own key back. If the server later refuses its key
    (an admin pressed Reset key), it enrolls again — which needs the fleet
    enrollment key, so it tells the server whether it holds one.
  • The server sets the rhythm (`next_poll_after_secs`); the agent obeys.
  • First call is offset by a hash of the fingerprint, retries back off with
    jitter — thousands of devices booting together never arrive together.
  • Order results are kept until a heartbeat carrying them is accepted — as
    many per heartbeat as fit the server's size limit, the rest on the next.
  • Any failure is logged once, on the transition, and the loop carries on.
  • Under systemd it exits when its own code changes on disk (a `git pull`),
    so the restart picks the new version up instead of the next reboot.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from pathlib import Path

import fms_protocol as protocol

from . import collect, commands, identity, transport
from .config import Config

log = logging.getLogger("fms_agent")

MIN_WAIT, MAX_WAIT, MAX_BACKOFF = 5, 3600, 300
ENROLL_GRACE_SECS = 600   # how long to wait for the edge app before enrolling without its edge_id


class Waiting(transport.Rejected):
    """Not an error — the agent is deliberately holding off (logged once)."""


def _code_stamp() -> int:
    """The newest modification time among the agent's own source files."""
    newest = 0
    for folder in (Path(__file__).parent, Path(protocol.__file__).parent):
        for f in folder.glob("*.py"):
            try:
                newest = max(newest, f.stat().st_mtime_ns)
            except OSError:                # replaced mid-update: the next look sees it
                pass
    return newest


def _size(value) -> int:
    return len(transport.encode(value))


def _shorten(result: dict, room: int) -> dict:
    """The same answer with only as much of the END of its output — the newest
    log lines — as fits in `room` bytes of request body."""
    tail = result.get("output") or ""
    while True:
        shorter = {**result, "output": "…(earlier output trimmed to fit a check-in)\n" + tail}
        excess = _size(shorter) - room
        if excess <= 0 or not tail:
            return shorter
        per_char = _size(tail) / len(tail)    # JSON escaping makes some characters cost more
        tail = tail[math.ceil(excess / per_char):]


class Agent:
    def __init__(self, cfg: Config, enroll_grace: float = ENROLL_GRACE_SECS) -> None:
        self.cfg = cfg
        self.enroll_grace = enroll_grace
        self.stop = threading.Event()
        self.fingerprint = identity.fingerprint(cfg.fingerprint)
        self.identity = identity.load(cfg.state_dir)
        self.clock_offset = 0.0          # server time − our time, used only for signing
        self.interval = 60.0
        self.rtt_ms: float | None = None
        self.results: list[dict] = []    # answers to orders, waiting to be delivered
        self._started = time.monotonic()
        self._last_problem: str | None = ""

    # ---- one exchange ---------------------------------------------------
    def beat(self) -> float:
        """One exchange with the server. Returns seconds until the next one."""
        status, status_error = collect.edge_status(self.cfg.status_url)
        if self.identity is None:
            return self._enroll(status)
        payload = {
            "protocol": protocol.VERSION,
            "agent": collect.facts(self.rtt_ms, self.interval, self.cfg.enroll_key),
            "status": status,
            "status_error": status_error,
            "results": [],
        }
        payload["results"] = carried = self._carry(payload)
        try:
            reply = self._post(protocol.HEARTBEAT_PATH, payload,
                               self.identity["edge_id"], self.identity["secret"])
        except transport.Rejected as exc:
            if exc.status == 401:        # key reset by an admin (or unknown here): enroll afresh
                return self._enroll(status)
            raise
        del self.results[:len(carried)]                       # delivered — drop them
        self.interval = min(max(float(reply.get("next_poll_after_secs", 60)), MIN_WAIT), MAX_WAIT)
        self.results += [commands.run(order) for order in reply.get("commands") or []]
        # Report results straight away instead of making the admin wait a cycle.
        return 1.0 if self.results else self.interval

    def _carry(self, payload: dict) -> list[dict]:
        """The answers this heartbeat takes along: oldest first, as many as fit the
        server's body limit beside the rest of `payload` — the others go on the
        next beats. One that cannot fit even on its own is cut down to its newest
        output: sent whole, it would be refused (413) with every check-in, forever."""
        room = protocol.MAX_BODY_BYTES - len(transport.encode(payload))
        carried: list[dict] = []
        for result in self.results[:protocol.MAX_RESULTS]:
            if not carried and _size(result) > room:
                result = self.results[0] = _shorten(result, room)
            room -= _size(result) + (1 if carried else 0)      # "," between list items
            if room < 0:
                break
            carried.append(result)
        return carried

    def _enroll(self, status: dict | None) -> float:
        if not self.cfg.enroll_key:
            raise Waiting("this device must enroll but has no fleet enrollment key (FMS_ENROLL_KEY) "
                          "— on a Jetson, run setup/install_fleet_agent.sh to give it one")
        if status is None and time.monotonic() - self._started < self.enroll_grace:
            raise Waiting("waiting for the edge app to answer, so its edge_id can be adopted")
        reported = (status or {}).get("edge_id") or ""
        try:
            reply = self._post(protocol.ENROLL_PATH, {
                "protocol": protocol.VERSION, "fingerprint": self.fingerprint,
                "edge_id": reported if isinstance(reported, str) else "",
                "agent": collect.facts(self.rtt_ms, self.interval, self.cfg.enroll_key),
            }, self.fingerprint, self.cfg.enroll_key)
        except transport.Rejected as exc:
            if exc.status == 409:
                raise Waiting("this device is already enrolled — an admin must press "
                              "Reset key in the fleet console to let it in again", 409) from None
            if exc.status == 403:
                raise Waiting("this device's access is revoked in the fleet console", 403) from None
            raise
        self.identity = {"edge_id": reply["edge_id"], "secret": reply["secret"],
                         "origin": reply.get("origin"), "enrolled_at": time.time()}
        identity.save(self.cfg.state_dir, self.identity)
        log.info("enrolled with the fleet as %s (%s)", reply["edge_id"], reply.get("origin"))
        return 1.0                        # first heartbeat right away

    def _post(self, path: str, payload: dict, principal: str, secret: str) -> dict:
        reply, self.rtt_ms = transport.post(self.cfg.url, path, payload, principal, secret,
                                            self.clock_offset)
        self.clock_offset = float(reply.get("server_time", time.time())) - time.time()
        return reply

    # ---- forever ----------------------------------------------------------
    def run_forever(self, exit_on_code_change: bool = False) -> None:
        wait = int(self.fingerprint, 16) % 30
        backoff, skewed = float(MIN_WAIT), False
        code = _code_stamp()
        log.info("fleet agent started — reporting to %s", self.cfg.url)
        while not self.stop.wait(wait):
            # Answers to orders go out first: a restart must not lose them.
            if exit_on_code_change and not self.results and _code_stamp() != code:
                log.info("fleet agent code changed on disk — exiting so it restarts on the new version")
                return
            try:
                wait = self.beat()
                backoff, skewed = float(MIN_WAIT), False
                self._report(None)
            except transport.ClockSkew as exc:
                self.clock_offset = exc.server_time - time.time()
                if not skewed:
                    log.warning("device clock is %.0fs off the server — correcting and retrying",
                                -self.clock_offset)
                wait, skewed = (1.0 if not skewed else backoff), True
            except Exception as exc:                       # never let the loop die
                self._report(exc)
                wait = backoff * random.uniform(0.8, 1.2)
                backoff = min(backoff * 2, MAX_BACKOFF)
        log.info("fleet agent stopped")

    def _report(self, exc: Exception | None) -> None:
        """Log the CHANGE, not every beat — the journal stays readable."""
        problem = None if exc is None else str(exc)
        if problem != self._last_problem:
            if problem is None:
                log.info("checking in with the fleet as %s (next in %.0fs)",
                         self.identity and self.identity["edge_id"], self.interval)
            elif isinstance(exc, Waiting):
                log.info("%s", problem)
            else:
                log.warning("cannot check in with the fleet: %s", problem)
        self._last_problem = problem
