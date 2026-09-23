from __future__ import annotations

"""
The ONE wording of an event. Every reader gets the same text: the cloud alert
(saveAlert messageText), the snapshot / fall-clip annotation (saveSnapshot
text), the local event store and the monitor's sync console.

Two lines — what happened and where, then when:

    Fall detected in Lounge
    8:33 AM, 9 Sep 2026

    No motion for 1 h 2 min in Lounge (Sofa)
    8:35 AM, 9 Sep 2026

    Moved from Kitchen to Lounge · with a visitor
    8:40 AM, 9 Sep 2026

Rules, each one a complaint about the old single line
('No motion 2/15 min · LOUNGE · Care · 8:33 AM, 09 Sep 2026'):
  * NEVER the care recipient's name. The account IS the recipient, so a name
    adds nothing, and on a visitor snapshot it claimed the frame showed them.
  * NOTHING TWICE. No severity prefix (the AlertType carries it), no camera
    number (saveSnapshot carries cameraNumber), and a move names its rooms once.
  * REAL DURATIONS. A stillness snapshot says how long it has lasted
    ('No motion for 1 h 2 min'), not which of the per-minute snapshots it is.
  * PLACES READ AS NAMES. 'LIVING_ROOM' -> 'Living Room'; a name typed in mixed
    case ('TV room') is left exactly as typed.

`event` is anything with the Event fields (schemas.event.Event), so tests can
pass a plain namespace.
"""

import re
from datetime import datetime

# Stillness signals, whose headline carries how long the condition has held.
_HELD = {"no_motion", "no_motion_snapshot", "no_transition_snapshot"}
# Moves, whose detail is 'from → to' and which name their own rooms/areas.
_MOVES = {"room_transition", "area_transition"}


def place(name: str | None) -> str:
    """A room or zone as a person would write it."""
    text = re.sub(r"[_\s]+", " ", name or "").strip()
    if text and (text.isupper() or text.islower()):
        text = " ".join(w[:1].upper() + w[1:].lower() for w in text.split())
    return text


def duration(secs: float) -> str:
    """'45 min', '1 h', '1 h 2 min' — to the nearest minute."""
    hours, mins = divmod(int(round(max(secs, 0.0) / 60.0)), 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} h {mins} min" if mins else f"{hours} h"


def stamp(when: datetime | str) -> str:
    """'8:33 AM, 9 Sep 2026' in the event's own (edge-local) time. A value that
    is not a datetime is shown as given rather than dropped."""
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return when
    return f"{when:%I:%M %p}".lstrip("0") + f", {when.day} {when:%b %Y}"


def headline(event) -> str:
    """Line 1: what happened, where, and who else was there."""
    et = (event.event_type or "").lower()
    title = event.title or et.replace("_", " ").capitalize()
    room, zone = place(event.room_name), place(event.zone_name)
    frm, _, to = (event.detail or "").partition("→")
    frm, to = place(frm), place(to)

    if et in _MOVES and frm and to:
        text = f"Moved from {frm} to {to}"
        if et == "area_transition" and room:
            text += f" in {room}"            # the areas are inside this room
    else:
        if et in _HELD and getattr(event, "duration_secs", None):
            title = f"{title} for {duration(event.duration_secs)}"
        where = f"{room} ({zone})" if room and zone else room or zone
        text = f"{title} in {where}" if where else title
        if et == "no_transition_snapshot" and event.detail:
            text += f" · still {event.detail}"          # the posture held
    if event.co_present:
        text += f" · {event.co_present}"
    return text


def describe(event, extra: str | None = None) -> str:
    """The full two-line text. `extra` joins line 1 (e.g. 'frame 2 of 3')."""
    line = headline(event) + (f" · {extra}" if extra else "")
    when = stamp(event.timestamp or "")
    return f"{line}\n{when}" if when else line
