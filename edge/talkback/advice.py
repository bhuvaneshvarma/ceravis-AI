from __future__ import annotations

"""
What to tell a person when a camera refuses the talk-back password — in ONE place.

This used to be written out separately in the protocol error, the readiness
message, the CLI and `diagnose`, and all four said the same wrong thing: "re-pair
the camera, it is holding an old copy". That was true once (2026-09-09, a camera
cut off from the internet after an account password change). It was not true on
2026-09-21, when both bench cameras refused the CURRENT password right after being
re-paired, with internet, and the code from the day it worked refused it too.

So the advice is now the checklist that actually separates the causes, in the
order that costs the least and risks the least, and every caller reads it from
here. Re-pairing is last: it can reset the camera's own stream account, its WiFi
and its address, and it did not help the one time we have measured.
"""

# For the carer and the live wall: one sentence, no jargon.
REFUSED_SHORT = ("The camera refused the TP-Link password. An installer needs to "
                 "check the Tapo app settings for this camera.")

# For the installer / technician: the checks, cheapest and safest first.
REFUSED_STEPS = (
    "In the Tapo app, Me > Tapo Lab > Third-Party Compatibility is ON.",
    "The camera belongs to the account whose password was entered — not shared "
    "with it from another account (a shared camera expects the OWNER's password).",
    "That account signs in with an email and password — not with Google or Apple.",
    "Enter that account's current password again (Setup > Cameras > Talk-back, or "
    "`python3 -m tools.talkback set`).",
    "Only if all of that is right: remove the camera in the Tapo app and add it "
    "again. This can reset the camera's stream password, WiFi and address.",
)


def refused_detail() -> str:
    """The technician's version as one line, for API `detail` fields and logs."""
    return ("The camera refused the TP-Link password. Check, in order: "
            + " ".join(f"({i}) {s}" for i, s in enumerate(REFUSED_STEPS, 1)))


def refused_lines(indent: str = "    ") -> str:
    """The technician's version as a numbered list, for the command line."""
    return "\n".join(f"{indent}{i}. {s}" for i, s in enumerate(REFUSED_STEPS, 1))
