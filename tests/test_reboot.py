"""
Reboot authorisation, safety deferral and accountability.

Nothing here reboots anything: `reboot.perform` is stubbed, and the assertion is
that the right decision was reached, not that the kernel went down.

Run:  PYTHONPATH=edge python tests/test_reboot.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))

from config.settings import settings          # noqa: E402
from maintenance import reboot                # noqa: E402


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


# Redirect every file the module writes into a throwaway dir, so a real device's
# password is never touched by a test run.
_TMP = Path(tempfile.mkdtemp(prefix="ceravis-reboot-test-"))
reboot._DATA = _TMP
reboot._AUTH_FILE = _TMP / "reboot_auth.json"
reboot._MARKER_FILE = _TMP / "last_reboot.json"


class _Outbox:
    def __init__(self, pending_alerts):
        self._n = pending_alerts

    def stats(self):
        return {"pending_alerts": self._n}


class _BrokenOutbox:
    def stats(self):
        raise RuntimeError("db locked")


print("\n1. password storage")
check("no password before one is set", not reboot.has_password())
ok, err = reboot.verify_password("anything")
check("verify fails cleanly with no password set", not ok and "no reboot password" in (err or ""))

try:
    reboot.set_password("short")
    weak_rejected = False
except ValueError:
    weak_rejected = True
check("a too-short password is rejected", weak_rejected)

reboot.set_password("correct-horse-battery")
check("password now set", reboot.has_password())

raw = json.loads(reboot._AUTH_FILE.read_text())
check("plaintext is NOT stored",
      "correct-horse-battery" not in json.dumps(raw))
check("stored as pbkdf2_sha256", raw.get("algo") == "pbkdf2_sha256", str(raw.get("algo")))
check("salt is per-device random", len(raw.get("salt") or "") > 10)

ok, err = reboot.verify_password("correct-horse-battery")
check("correct password verifies", ok, err or "")
ok, _ = reboot.verify_password("wrong")
check("wrong password rejected", not ok)


print("\n2. brute-force lockout")
reboot.set_password("correct-horse-battery")          # resets counters
for _ in range(settings.reboot_max_attempts):
    reboot.verify_password("wrong")
ok, err = reboot.verify_password("correct-horse-battery")
check("locked out after N failures even with the RIGHT password",
      not ok and "locked" in (err or "").lower(), err or "")
check("lockout reports remaining time", reboot.lockout_remaining() > 0,
      f"{reboot.lockout_remaining():.0f}s")

# a lockout that has expired must let a correct password through again
raw = json.loads(reboot._AUTH_FILE.read_text())
raw["locked_until"] = None
reboot._write_auth(raw)
ok, _ = reboot.verify_password("correct-horse-battery")
check("works again once the lockout lapses", ok)


print("\n3. safety deferral")
prev = settings.reboot_defer_on_pending_alerts
settings.reboot_defer_on_pending_alerts = True
check("clear when nothing is queued", reboot.safety_block(_Outbox(0)) is None)
block = reboot.safety_block(_Outbox(2))
check("BLOCKED while alerts are undelivered", block is not None and "2" in block, block or "")
check("no outbox to consult is not a block", reboot.safety_block(None) is None)
check("a BROKEN outbox never blocks (fail open, not stuck)",
      reboot.safety_block(_BrokenOutbox()) is None)
settings.reboot_defer_on_pending_alerts = False
check("deferral can be turned off", reboot.safety_block(_Outbox(9)) is None)
settings.reboot_defer_on_pending_alerts = prev


print("\n4. accountability marker")
calls: list = []
real_run = reboot.subprocess.run
reboot.subprocess.run = lambda *a, **k: calls.append(a)      # never actually reboot
try:
    reboot.perform("unit test", "pytest", delay_secs=0.01)
    check("marker written BEFORE the reboot fires", reboot._MARKER_FILE.exists())
    marker = json.loads(reboot._MARKER_FILE.read_text())
    check("marker records the reason", marker.get("reason") == "unit test", str(marker))
    check("marker records the actor", marker.get("actor") == "pytest")
    time.sleep(0.2)
    check("the reboot command was invoked", len(calls) == 1, f"{len(calls)} call(s)")
finally:
    reboot.subprocess.run = real_run

report = reboot.boot_report()
check("boot_report returns the marker", report is not None and report.get("reason") == "unit test")
check("marker is CLEARED so it reports once only", not reboot._MARKER_FILE.exists())
check("boot_report is None when nothing rebooted us", reboot.boot_report() is None)


print("\n5. status surface")
st = reboot.status(_Outbox(0))
check("reports whether a password is set", st["password"]["set"] is True)
check("reports safe_to_reboot", st["safe_to_reboot"] is True)
check("reports the scheduled window", "03:00" in st["scheduled"]["window"],
      st["scheduled"]["window"])
st = reboot.status(_Outbox(1))
check("status reflects a block", st["safe_to_reboot"] is False and st["blocked_reason"])


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll reboot checks passed.")
