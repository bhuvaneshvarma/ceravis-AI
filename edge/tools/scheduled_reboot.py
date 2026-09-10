#!/usr/bin/env python3
"""
The 03:00 reboot, as systemd runs it (ceravis-reboot.timer -> .service).

Deliberately does NOT go through the HTTP API. Talking to the running service
would mean either punching an unauthenticated hole in the password-gated reboot
endpoint or shipping a second credential onto the box — both worse than simply
reading the same files the service does. This process opens the same SQLite
outbox and appends to the same console log, so operator and timer see one
consistent story.

Exit codes are for `systemctl status` / journalctl:
  0  rebooting, or deliberately skipped tonight (both are correct outcomes)
  1  something failed and no decision could be made
"""
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


def main() -> int:
    if not settings.reboot_scheduled_enabled:
        log.info("scheduled reboot is disabled (REBOOT_SCHEDULED_ENABLED=false)")
        return 0

    # --- clock trust (RTC-less hardware) --------------------------------------
    # This board boots at 1970 until it is disciplined over the network, and a
    # wall-clock timer can then fire at a random daytime moment (see
    # maintenance/reboot.py). Refuse to reboot unless the clock is sane AND it is
    # genuinely the nightly window AND we are not seconds past a boot — otherwise
    # a clock step would reboot the device mid-day, cold-boot back into 1970 and
    # loop. None of these can false-skip a healthy night. Skipping is always a
    # correct outcome (exit 0): a missed night is just a missed night.
    now_local = reboot.clock.now()
    if not reboot.clock_trustworthy(now_local):
        log.warning("SKIPPING reboot — clock reads %s (year %d); it has not been "
                    "corrected from the RTC-less boot clock yet",
                    now_local.isoformat(timespec="seconds"), now_local.year)
        call_log.record(
            "event", True,
            label="INFO · Nightly reboot skipped · clock still on the 1970 boot value")
        return 0
    if not reboot.in_reboot_window(now_local):
        log.warning("SKIPPING reboot — fired at %s, outside the %02d:00–%02d:00 "
                    "window; this is a clock step, not the schedule",
                    now_local.isoformat(timespec="seconds"),
                    settings.reboot_window_start_hour % 24,
                    (settings.reboot_window_start_hour + 1) % 24)
        call_log.record(
            "event", True,
            label=("INFO · Nightly reboot skipped · fired outside the window at "
                   + now_local.strftime("%H:%M") + " (clock step, not schedule)"))
        return 0
    up = reboot.uptime_secs()
    if up is not None and up < settings.reboot_min_uptime_secs:
        log.warning("SKIPPING reboot — only up %.0fs (< %.0fs); too soon after a "
                    "boot to reboot again (loop backstop)",
                    up, settings.reboot_min_uptime_secs)
        call_log.record(
            "event", True,
            label=f"INFO · Nightly reboot skipped · only {up:.0f}s since boot")
        return 0

    block = reboot.safety_block(_outbox())
    if block:
        log.warning("SKIPPING tonight's reboot — %s", block)
        call_log.record("event", True,
                        label=f"INFO · Nightly reboot skipped · {block}")
        return 0

    log.info("safety check clear — rebooting")
    # delay_secs=0: nothing is waiting on an HTTP response here, and systemd has
    # already captured the log line.
    reboot.perform("scheduled nightly", "systemd timer", delay_secs=0.0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("scheduled reboot failed")
        sys.exit(1)
