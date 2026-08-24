#!/usr/bin/env python3
"""
Set the reboot password for this device.

Run on the device:  python3 setup/set_reboot_password.py

The password is typed, never passed as an argument — an argument lands in the
shell history and in `ps` output for every user on the box. Only a PBKDF2-SHA256
hash is written (edge/data/reboot_auth.json, mode 0600); the plaintext is never
stored and never logged.

This gates the MANUAL reboot endpoint. The nightly timer does not use it —
nobody is present at 03:00, and its authorisation is that root owns systemd.
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))

from maintenance import reboot          # noqa: E402


def main() -> int:
    if reboot.has_password():
        print("A reboot password is already set on this device.")
        if input("Replace it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Unchanged.")
            return 0

    try:
        first = getpass.getpass("New reboot password: ")
        second = getpass.getpass("Confirm: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if first != second:
        print("Passwords do not match — nothing was changed.")
        return 1

    try:
        reboot.set_password(first)
    except ValueError as exc:
        print(f"Rejected: {exc}")
        return 1

    print(f"Reboot password set. Stored as a PBKDF2-SHA256 hash in "
          f"{reboot._AUTH_FILE}")
    print("\nTest it without rebooting:")
    print("  curl -s localhost:8000/api/v1/system/reboot | python3 -m json.tool")
    return 0


if __name__ == "__main__":
    sys.exit(main())
