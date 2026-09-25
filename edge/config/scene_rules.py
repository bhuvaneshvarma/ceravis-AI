from __future__ import annotations

"""
Day and night rule sets — two COMPLETE sets of thresholds for ONE engine.

A lit room and an infrared room run exactly the same identity / lock / crop /
visitor code. What differs between them is the NUMBERS and a few policies, and
those are chosen here: every camera, every tick, reads its rules from the set
for its current modality (ingestion/illumination.py is the one owner of that
fact, with hysteresis — the set switches when the picture really changes).

  DAY    the settings as declared (REID_MATCH_THRESHOLD, ...) — the daylight
         rules, restored to their values from before night vision existed.
  NIGHT  a clone of DAY overlaid by every `night_<name>` setting, i.e. the env
         key NIGHT_<NAME>. Anything night does not override is inherited from
         DAY, so the night set is always complete; tuning a value for the dark
         never moves the daytime number, and vice versa.

The shape is the day/night profile of a camera's image processor, or a config
overlay: one engine, the scene picks the parameter set. Values are read live
(nothing cached), so an operator or a test changing a setting takes effect on
the next tick.

What stays OUTSIDE the rule sets is data, not rules: which gallery an
embedding is compared with, and how a crop is prepared, follow the modality
itself (reid/faiss_index.py, ingestion/illumination.monochrome).
"""

from config.settings import settings

_UNSET = object()


class RuleSet:
    """Attribute access to one scene's rules: `rules.reid_match_threshold`."""

    __slots__ = ("name", "_prefix")

    def __init__(self, name: str, prefix: str = "") -> None:
        self.name = name
        self._prefix = prefix

    def __getattr__(self, key: str):
        if self._prefix:
            v = getattr(settings, self._prefix + key, _UNSET)
            if v is not _UNSET:
                return v
        return getattr(settings, key)

    def __repr__(self) -> str:
        return f"RuleSet({self.name})"


DAY = RuleSet("day")
NIGHT = RuleSet("night", "night_")


def for_ir(ir: bool) -> RuleSet:
    return NIGHT if ir else DAY


def for_camera(camera_id: str) -> RuleSet:
    """The rule set a camera runs on right now."""
    from ingestion import illumination
    return for_ir(illumination.is_ir(camera_id))
