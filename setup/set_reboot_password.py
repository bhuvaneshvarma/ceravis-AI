#!/usr/bin/env python3
"""
Set the reboot password for this device.

Run on the device:  sudo python3 setup/set_reboot_password.py
       production:  sudo /opt/ceravis/current/venv/bin/python \
                         /opt/ceravis/current/setup/set_reboot_password.py
       installer:   ... set_reboot_password.py --stdin --replace   (one line on stdin)

The password is typed (or piped), never passed as an argument — an argument
lands in the shell history and in `ps` output for every user on the box. Only a
PBKDF2-SHA256 hash is written, into the device's data folder (the same one the
service uses: DATA_DIR, else /etc/ceravis/layout.env, else edge/data), mode
0600 and owned by the account the service runs as; the plaintext is never
stored and never logged.

This gates the MANUAL reboot endpoint. The nightly timer does not use it —
nobody is present at 03:00, and its authorisation is that root owns systemd.
"""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))

from maintenance import reboot          # noqa: E402


def _hand_to_service(path: Path) -> None:
    """Run as root, the new file would be root's and the service could not read
    it: give it the data folder's owner (the service account)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0 and path.exists():
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)


def main(argv: list[str]) -> int:
    piped, replace = "--stdin" in argv, "--replace" in argv
    if reboot.has_password() and not replace:
        print("A reboot password is already set on this device.")
        if piped or input("Replace it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Unchanged.")
            return 0

    if piped:
        first = second = sys.stdin.readline().rstrip("\r\n")
    else:
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
    _hand_to_service(reboot._AUTH_FILE)

    print(f"Reboot password set. Stored as a PBKDF2-SHA256 hash in "
          f"{reboot._AUTH_FILE}")
    if not piped:
        print("\nTest it without rebooting:")
        print("  curl -s localhost:8000/api/v1/system/reboot | python3 -m json.tool")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
