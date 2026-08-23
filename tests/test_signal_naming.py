"""
One signal, one name.

`no_motion` carried THREE names at once: the internal event type `no_motion`,
the operator-facing title "No movement", and the wire AlertType `NO_MOTION`.
The monitor showed "No movement", so bench testing for "no motion" looked like
the signal was never firing when it was — a naming drift that cost real
debugging time.

This is a STATIC check (no TRT, no FAISS, no camera): it reads the source, so it
runs anywhere and cannot be skipped by a missing engine.

Run:  PYTHONPATH=edge python tests/test_signal_naming.py
"""
import ast
import io
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"

# The wire value the backend's AlertType enum expects. NOT display text —
# changing it breaks saveAlert, so it is pinned here deliberately.
WIRE_ALERT_TYPE = "NO_MOTION"
DISPLAY = "No motion"
BANNED = re.compile(r"no\s+movement", re.IGNORECASE)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("\n1. no source in edge/ says 'No movement'")
hits = []
for py in sorted(EDGE.rglob("*.py")):
    if "__pycache__" in py.parts:
        continue
    for n, line in enumerate(io.open(py, encoding="utf-8", errors="replace"), 1):
        if BANNED.search(line):
            hits.append(f"{py.relative_to(ROOT)}:{n}")
for html in sorted(EDGE.rglob("*.html")):
    for n, line in enumerate(io.open(html, encoding="utf-8", errors="replace"), 1):
        if BANNED.search(line):
            hits.append(f"{html.relative_to(ROOT)}:{n}")
check("no 'No movement' anywhere in edge/", not hits, ", ".join(hits[:4]))


print("\n2. the enricher titles every no_motion event 'No motion'")
src = io.open(EDGE / "events" / "event_enricher.py", encoding="utf-8").read()
tree = ast.parse(src)
alert_map: dict[str, tuple[str, str]] = {}
for node in ast.walk(tree):
    # _ALERT_MAP carries a type annotation, so it is an AnnAssign (one .target)
    # rather than an Assign (.targets) — handle both, or this silently finds
    # nothing and every check below "passes" against an empty dict.
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.Assign):
        targets = node.targets
    else:
        continue
    if any(getattr(t, "id", "") == "_ALERT_MAP" for t in targets) \
            and isinstance(node.value, ast.Dict):
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Tuple):
                sev, title = (e.value for e in v.elts)
                alert_map[k.value] = (sev, title)
check("_ALERT_MAP parsed", bool(alert_map), f"{len(alert_map)} entries")

motion_keys = [k for k in alert_map if k.startswith("no_motion")]
check("no_motion event types present", len(motion_keys) >= 2, ", ".join(motion_keys))
for k in motion_keys:
    check(f"'{k}' titled '{DISPLAY}'", alert_map[k][1] == DISPLAY, alert_map[k][1])

check("the alert itself is CRITICAL",
      alert_map.get("no_motion", ("", ""))[0] == "critical",
      alert_map.get("no_motion", ("?", "?"))[0])
check("the follow-up snapshot is INFO",
      alert_map.get("no_motion_snapshot", ("", ""))[0] == "info",
      alert_map.get("no_motion_snapshot", ("?", "?"))[0])


print("\n3. no_transition stays a DISTINCT signal (not folded into no motion)")
nt = alert_map.get("no_transition_snapshot")
check("no_transition_snapshot still present", nt is not None)
if nt:
    check("titled 'No transition'", nt[1] == "No transition", nt[1])
    check("severity is info — snapshots only, never an alert",
          nt[0] == "info", nt[0])


print("\n4. the cloud wire contract is unchanged")
pub = io.open(EDGE / "alerts" / "cloud_alert_publisher.py", encoding="utf-8").read()
check(f"AlertType is still {WIRE_ALERT_TYPE}",
      f'"{WIRE_ALERT_TYPE}"' in pub)
check("no_motion maps to it explicitly",
      re.search(r'"no_motion"\s*:\s*"NO_MOTION"', pub) is not None)
check("only no_motion is alert-tier, no_transition is snapshot-only",
      "no_transition" not in re.search(
          r"cloud_alert_event_types.*?\n",
          io.open(EDGE / "config" / "settings.py", encoding="utf-8").read()
      ).group(0))


print("\n5. the operator line renders 'No motion'")
check("burst line says 'No motion'", 'f"No motion {det} min"' in pub)


if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll signal-naming checks passed.")
