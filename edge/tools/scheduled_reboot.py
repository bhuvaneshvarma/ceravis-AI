#!/usr/bin/env python3
"""
The 03:00 reboot, as systemd runs it (ceravis-reboot.timer -> .service).

Deliberately does NOT go through the HTTP API. Talking to the running service
would mean either punching an unauthenticated hole in the password-gated reboot
endpoint or shipping a second credential onto the box — both worse than simply
reading the same files the service does. This process opens the same SQLite
outbox and appends to the same console log, so operator and timer see one
consistent story.

Usage:
  scheduled_reboot.py            # the real run: reboot if every gate is clear
  scheduled_reboot.py --explain  # print the decision + every gate, NEVER reboot
  scheduled_reboot.py --force    # reboot NOW, bypassing the time-of-day gates
                                 # (still honours the safety deferral) — the
                                 # end-to-end "does it actually reboot" test

Exit codes are for `systemctl status` / journalctl:
  0  rebooting, or deliberately skipped tonight (both are correct outcomes)
  1  something failed and no decision could be made
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # edge/ on the path

from config.settings import settings          # noqa: E402
from integration import call_log              # noqa: E402
from maintenance import reboot                # noqa: E402


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduled-reboot")


def _outbox():
    """The same durable queue the service uses — opened read-only from here."""
    try:
        from storage.outbox_store import OutboxStore
        from storage.sqlite_store import SqliteStore
        return OutboxStore(SqliteStore(settings.sqlite_path))
    except Exception:
        log.exception("could not open the outbox — proceeding without the "
                      "safety check rather than skipping the reboot entirely")
        return None


def _explain(decision: dict) -> int:
    """Print the decision and every gate, and reboot NOTHING. The tool that ends
    the guessing: run it on the device and read exactly what tonight's run will
    do, with the real clock, window, uptime and outbox."""
    verdict = ("WOULD REBOOT" if decision["reboot"]
               else "WOULD REFRESH" if decision.get("refresh") else "WOULD SKIP")
    print(f"\n  scheduled reboot decision: {verdict}")
    print(f"  now (device-local): {decision['now_local']}")
    up = decision["uptime_secs"]
    print(f"  uptime: {up:.0f}s" if up is not None else "  uptime: unknown")
    print(f"  window: {settings.reboot_window_start_hour % 24:02d}:00–"
          f"{(settings.reboot_window_start_hour + 1) % 24:02d}:00 (+grace)")
    print("  gates:")
    for name, ok in decision["gates"].items():
        print(f"    {'PASS' if ok else 'FAIL'}  {name}")
    print(f"  => {decision['reason']}\n")
    # Also dump machine-readable, so it can be grepped/piped.
    print(json.dumps(decision, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    explain = "--explain" in argv or "--dry-run" in argv
    force = "--force" in argv

    if not settings.reboot_scheduled_enabled and not force:
        log.info("scheduled reboot is disabled (REBOOT_SCHEDULED_ENABLED=false)")
        if explain:
            return _explain(reboot.scheduled_decision(outbox=_outbox()))
        return 0

    # ONE decision, evaluated once (see maintenance/reboot.scheduled_decision):
    # the clock is sane (not the 1970 boot clock), it is really the 03:00–04:00
    # window (not a daytime clock step), we are not seconds past a boot (loop
    # backstop), and no alert is queued. None of these can false-skip a healthy
    # night. Skipping is always a correct outcome (exit 0).
    decision = reboot.scheduled_decision(outbox=_outbox())

    if explain:
        return _explain(decision)

    if force:
        # The end-to-end execution test: bypass the time-of-day gates but keep
        # the safety deferral, then reboot through the REAL path. Proves the
        # reboot actually happens, at any hour, without waiting for 03:00.
        block = reboot.safety_block(_outbox())
        if block:
            log.warning("--force: NOT rebooting — %s", block)
            return 0
        log.warning("--force: rebooting NOW (time-of-day gates bypassed)")
        reboot.perform("forced test", "operator (--force)", delay_secs=0.0)
        return 0

    if decision.get("refresh"):
        log.info("all gates clear — refreshing (the weekly reboot is %.1f "
                 "day(s) away)", max(0.0, settings.reboot_interval_days
                                      - (decision["uptime_secs"] or 0.0) / 86400.0))
        from maintenance import refresh
        refresh.perform("scheduled nightly", "systemd timer")
        return 0

    if not decision["reboot"]:
        log.warning("SKIPPING reboot — %s", decision["reason"])
        call_log.record(
            "event", True,
            label=f"INFO · Nightly reboot skipped · {decision['reason']}")
        return 0

    log.info("all gates clear — rebooting")
    # delay_secs=0: nothing is waiting on an HTTP response here, and systemd has
    # already captured the log line. perform() runs the reboot SYNCHRONOUSLY at
    # delay 0 (a daemon timer would be killed when this short-lived script exits).
    reboot.perform("scheduled nightly", "systemd timer", delay_secs=0.0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("scheduled reboot failed")
        sys.exit(1)
