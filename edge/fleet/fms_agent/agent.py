"""The loop: observe every minute (logs) · (enroll once) → check in → act, forever.

Rules it lives by:
  • Every minute it measures every part of the device and keeps the record on
    the device (logs/health.jsonl, events.jsonl — fms_agent.logs), whether or
    not a fleet server is configured or reachable. A check-in sends the short
    summary of the newest record (fms_agent.health).
  • No per-device setup. With no identity yet it ENROLLS by itself, once: it
    sends who this device is (its name, board, release, disk) and gets back its
    permanent edge_id — the FMS is the only place edge_ids come from — and a
    secret. From the secret and the fleet enrollment key it derives its
    HEARTBEAT KEY, the value the FMS keeps (fms_protocol.heartbeat_key); it
    keeps that and forgets the secret.
  • It never enrolls again by itself. A refused check-in is logged, reported
    and retried with back-off (5 s doubling to 5 min); enrolling again is a
    person's decision (remove identity.json, restart the agent). An enrollment
    reply it cannot trust or use STOPS all check-ins until the agent restarts:
    the FMS may have registered the device anyway, and trying again could only
    register it again (the bench, 2026-11-13).
  • The server sets the rhythm (`next_poll_after_secs`); the agent obeys.
  • An enrolled device's first call is offset by a hash of its fingerprint,
    retries back off with jitter — thousands of devices booting together never
    arrive together.
  • Order results are kept until a heartbeat carrying them is accepted — as
    many per heartbeat as fit the server's size limit, the rest on the next.
  • A problem is logged once, on the change (journal + events.jsonl), and the
    next heartbeat carries it (services.fleet_agent); the loop carries on.
  • Under systemd it exits when its own code changes on disk (a `git pull`),
    so the restart picks the new version up instead of the next reboot.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from pathlib import Path

import fms_protocol as protocol

from . import collect, commands, identity, transport
from . import health as health_report
from .config import Config
from .logs import DeviceLogs

log = logging.getLogger("fms_agent")

MIN_WAIT, MAX_WAIT, MAX_BACKOFF = 5, 3600, 300
OBSERVE_SECS = 60.0          # one full record of the device each minute
FRESH_SECS = 10.0            # a check-in reuses a record younger than this
LOG_OUTPUT_CHARS = 2000      # of an order's output, in fleet.jsonl's copy of a check-in
_NOT_AGAIN = ("the FMS may have registered this device anyway, so it is not enrolled again — "
              "check the FMS, then restart this agent (sudo systemctl restart ceravis-fleet-agent)")


class Waiting(transport.Rejected):
    """Not an error — the agent is deliberately holding off (logged once)."""


class Halted(transport.Rejected):
    """Check-ins stop until the agent is restarted: going on could only repeat a
    mistake on the server (register this device again), or cannot work at all."""


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


def _logged(payload: dict) -> dict:
    """A check-in's body as sent, for fleet.jsonl. Only an order's long output (a
    `logs` order returns whole log files) is cut to its end here — copied whole,
    the log would copy itself into itself; the FMS got it whole."""
    results = []
    for result in payload["results"]:
        output = result.get("output") or ""
        if len(output) > LOG_OUTPUT_CHARS:
            result = {**result, "output": f"…({len(output)} characters sent; the last "
                                          f"{LOG_OUTPUT_CHARS} here)\n" + output[-LOG_OUTPUT_CHARS:]}
        results.append(result)
    return {**payload, "results": results}


def _readable(answer: str | None):
    """The server's answer as JSON when it is a JSON object, else the text itself."""
    try:
        value = json.loads(answer) if answer else None
    except ValueError:
        return answer
    return value if isinstance(value, dict) else answer


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self.fingerprint = identity.fingerprint(cfg.fingerprint)
        self.identity = identity.load(cfg.state_dir)
        self.halted: str | None = None   # why check-ins stopped (until a restart)
        self.problem: str | None = None  # the last check-in's problem; None = fine
        self.clock_offset = 0.0          # server time − our time, used only for signing
        self.interval = 60.0
        self.rtt_ms: float | None = None
        self.results: list[dict] = []    # answers to orders, waiting to be delivered
        self.health = health_report.Health()
        self.logs = DeviceLogs(collect.data_dir())
        self.detail: dict | None = None  # the newest full record …
        self.status: dict | None = None  # … and the heartbeat summary built from it
        self._observed_at = -1e9
        self._reported: str | None = ""  # the problem last logged ("" = nothing yet)
        self._adopt_heartbeat_key()

    def _adopt_heartbeat_key(self) -> None:
        """An identity from an agent before 0.8.0 holds the enrollment's raw
        `secret`: derive the heartbeat key from it once, keep that, drop the
        secret. Without the fleet enrollment key it cannot be derived, and a
        heartbeat could only be refused: check-ins stop, with the reason."""
        if not self.identity or self.identity.get("heartbeat_key"):
            return
        if not self.cfg.enroll_key:
            self.halted = ("this device has no heartbeat key, and no fleet enrollment key to derive "
                           "it from — run setup/install_fleet_agent.sh with the key")
            return
        self.identity["heartbeat_key"] = protocol.heartbeat_key(self.cfg.enroll_key,
                                                                self.identity.pop("secret"))
        try:
            identity.save(self.cfg.state_dir, self.identity)
            log.info("heartbeat key derived from the stored enrollment secret; the secret is no longer kept")
        except OSError as exc:                       # kept in memory; saved on the next start
            log.warning("heartbeat key derived but not saved (%s)", exc)

    # ---- each minute ------------------------------------------------------
    def observe(self) -> None:
        """Measure every part of the device; keep the record on the device."""
        # Generous: the app's full report reads NetworkManager and the radio too.
        app, app_error = collect.edge_status(self.cfg.health_url, timeout=20.0)
        self.detail = self.health.observe(app, app_error, self._facts())
        self.status = self.health.summary(self.detail)
        self.logs.observation(self.detail, self.status)
        self._observed_at = time.monotonic()

    def _facts(self) -> dict:
        """The agent's own link to the FMS as it is NOW (its last problem)."""
        return health_report.agent_facts(self.rtt_ms, self.interval, self.cfg.enroll_key,
                                         self.halted or self.problem, stopped=bool(self.halted))

    # ---- one exchange ---------------------------------------------------
    def beat(self) -> float:
        """One exchange with the server. Returns seconds until the next one."""
        if self.halted:
            raise Halted(self.halted)
        if self.identity is None:
            status, _ = collect.edge_status(self.cfg.status_url)
            return self._enroll(status)
        # Every part of the device, each with its state and warnings
        # (fms_agent.health, docs/fleet-heartbeat-api.md) — from a fresh record.
        if time.monotonic() - self._observed_at > FRESH_SECS:
            self.observe()
        detail = self.detail
        facts = self._facts()
        if detail["agent"] != facts:      # a record reused: the agent's own state is today's
            detail["agent"] = facts
            self.status = self.health.summary(detail)
        payload = {"status": self.status, "results": []}
        payload["results"] = carried = self._carry(payload)
        try:
            reply = self._post(protocol.HEARTBEAT_PATH, payload,
                               self.identity["edge_id"], self.identity["heartbeat_key"])
        except Exception as exc:    # refused, unreachable or untrusted: logged; the loop backs off
            self.logs.exchange("heartbeat", False, status=getattr(exc, "status", None), error=str(exc),
                               request=_logged(payload), reply=_readable(getattr(exc, "reply", None)))
            raise
        self.health.delivered(detail)
        del self.results[:len(carried)]                       # delivered — drop them
        self.interval = min(max(float(reply.get("next_poll_after_secs", 60)), MIN_WAIT), MAX_WAIT)
        orders = reply.get("commands") or []
        self.logs.exchange("heartbeat", True, rtt_ms=self.rtt_ms, state=self.status["state"],
                           warning_count=self.status["warning_count"], results_sent=len(carried),
                           orders=len(orders), next_secs=self.interval,
                           request=_logged(payload), reply=reply)
        for order in orders:
            result = commands.run(order)
            self.logs.order(order, result)
            self.results.append(result)
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
        device_id = collect.device_id()
        try:
            reply = self._post(protocol.ENROLL_PATH, {
                "device_id": device_id,
                "hardware": collect.hardware(),
                "software": collect.software(),
                "agent": collect.facts(self.rtt_ms, self.interval, self.cfg.enroll_key),
                "status": status,
            }, None, self.cfg.enroll_key)            # None: the 4-line enroll signature
        except transport.UntrustedReply as exc:
            self.logs.exchange("enroll", False, error=str(exc))
            raise Halted(f"{exc}; {_NOT_AGAIN}") from None
        except transport.Rejected as exc:
            self.logs.exchange("enroll", False, status=exc.status, error=str(exc))
            if exc.status == 409:
                raise Waiting("this device is already enrolled — an admin must press "
                              "Reset key in the fleet console to let it in again", 409) from None
            if exc.status == 403:
                raise Waiting("this device's access is revoked in the fleet console", 403) from None
            raise
        # The edge_id becomes every route and link of this device: nothing of a
        # reply that is not plainly this device's is kept.
        edge_id, secret = reply.get("edge_id"), reply.get("secret")
        unusable = None
        if not collect.routable_edge_id(edge_id) or not isinstance(secret, str) or not secret:
            unusable = "the enrollment reply carries no valid edge_id and key — not saved"
        elif reply.get("device_id") not in (None, device_id):
            unusable = (f"the enrollment reply is for {reply['device_id']!r}, "
                        f"not this device ({device_id}) — not saved")
        if unusable:
            self.logs.exchange("enroll", False, error=unusable)
            raise Halted(f"{unusable}; {_NOT_AGAIN}")
        # The secret itself is not kept: only the heartbeat key derived from it.
        self.identity = {"edge_id": edge_id, "heartbeat_key": protocol.heartbeat_key(self.cfg.enroll_key, secret),
                         "device_id": device_id, "key_version": reply.get("key_version"),
                         "origin": reply.get("origin"), "enrolled_at": time.time()}
        identity.save(self.cfg.state_dir, self.identity)
        self.logs.exchange("enroll", True, edge_id=edge_id, origin=reply.get("origin"))
        warnings = reply.get("warnings")
        for note in warnings if isinstance(warnings, list) else []:
            log.warning("fleet: %s", note)
        log.info("enrolled with the fleet as %s%s", edge_id,
                 f" ({reply['origin']})" if reply.get("origin") else "")
        return 1.0                        # first heartbeat right away

    def _post(self, path: str, payload: dict, principal: str | None, secret: str) -> dict:
        reply, self.rtt_ms = transport.post(self.cfg.url, path, payload, principal, secret,
                                            self.clock_offset)
        self.clock_offset = float(reply.get("server_time", time.time())) - time.time()
        return reply

    # ---- forever ----------------------------------------------------------
    def run_forever(self, exit_on_code_change: bool = False, check_in: bool = True) -> None:
        """Observe every minute; with `check_in`, also report to the fleet server.
        A device still to enroll does so at once (the installer waits on it); an
        enrolled one staggers its first heartbeat."""
        now = time.monotonic()
        next_beat = now + (0 if self.identity is None else int(self.fingerprint, 16) % 30)
        backoff, skewed = float(MIN_WAIT), False
        code = _code_stamp()
        log.info("fleet agent started — %s", f"reporting to {self.cfg.url}" if check_in
                 else "keeping this device's health record (not in a fleet)")
        if check_in and self.halted:
            self._report(Halted(self.halted))
        while True:
            next_look = self._observed_at + OBSERVE_SECS
            checking = check_in and not self.halted
            due = min(next_beat, next_look) if checking else next_look
            if self.stop.wait(max(0.0, due - time.monotonic())):
                break
            # Answers to orders go out first: a restart must not lose them.
            if exit_on_code_change and not self.results and _code_stamp() != code:
                log.info("fleet agent code changed on disk — exiting so it restarts on the new version")
                break
            if checking and time.monotonic() >= next_beat:
                wait = backoff
                try:
                    wait = self.beat()
                    backoff, skewed = float(MIN_WAIT), False
                    self._report(None)
                except Halted as exc:                      # no more check-ins until a restart
                    self.halted = str(exc)
                    self._report(exc)
                except transport.ClockSkew as exc:
                    self.clock_offset = exc.server_time - time.time()
                    if not skewed:
                        log.warning("device clock is %.0fs off the server — correcting and retrying",
                                    -self.clock_offset)
                    wait, skewed = (1.0 if not skewed else backoff), True
                except Exception as exc:                   # never let the loop die
                    self._report(exc)
                    wait = backoff * random.uniform(0.8, 1.2)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                next_beat = time.monotonic() + wait
            if time.monotonic() - self._observed_at >= OBSERVE_SECS:
                try:
                    self.observe()
                except Exception:                          # a record lost, never the loop
                    log.exception("health record failed")
                    self._observed_at = time.monotonic()
        self.logs.stopped()
        log.info("fleet agent stopped")

    def _report(self, exc: Exception | None) -> None:
        """Log the CHANGE, not every beat — the journal and events.jsonl stay
        readable; the next heartbeat carries the problem (services.fleet_agent)."""
        problem = self.problem = None if exc is None else str(exc)
        if problem != self._reported:
            self.logs.check_in(problem, stopped=isinstance(exc, Halted))
            if problem is None:
                log.info("checking in with the fleet as %s (next in %.0fs)",
                         self.identity and self.identity["edge_id"], self.interval)
            elif isinstance(exc, Halted):
                log.error("fleet agent STOPPED checking in: %s", problem)
            elif isinstance(exc, Waiting):
                log.info("%s", problem)
            else:
                log.warning("cannot check in with the fleet: %s", problem)
        self._reported = problem
