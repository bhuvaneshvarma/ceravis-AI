from __future__ import annotations

"""
The ONE Format-A alert/snapshot line builder:

    '<head> · Camera 1* Kitchen / stove · Ravi · 11:45 AM, 23 Jun 2026'

Used by the live cloud publisher AND tests/test_cloud.py, so a replayed test
label is identical in shape to what production emits.
"""

from datetime import datetime


def camera_label(cam) -> str:
    """'Camera 1* ' with the bathroom (*) / main-egress (&) designators, or ''.
    `cam` is a camera-config object; None-safe."""
    if cam is None or not getattr(cam, "camera_number", None):
        return ""
    sym = ("*" if cam.has_bathroom_entrance else "") \
        + ("&" if cam.is_main_egress else "")
    return f"Camera {cam.camera_number}{sym} "


def format_line(head: str, cam, room_name: str | None, who: str,
                when: datetime | str, zone_name: str | None = None) -> str:
    """head · Camera N* Room / zone · who · h:mm AM, d Mon YYYY.
    `when` may be a datetime or an already-formatted/raw string."""
    loc = camera_label(cam) + " / ".join(p for p in (room_name, zone_name) if p)
    ts = (when.strftime("%I:%M %p, %d %b %Y").lstrip("0")
          if isinstance(when, datetime) else str(when))
    parts = [head]
    if loc:
        parts.append(loc)
    parts += [who, ts]
    return " · ".join(parts)
