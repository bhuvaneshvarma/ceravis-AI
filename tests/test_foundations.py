"""
The three foundations every other module stands on:

1. Device identity — the name made from the Jetson serial is unique for every
   different serial (a one-to-one encoding, not a hash), stable, and valid as a
   hostname and a Wi-Fi SSID.
2. Paths — every location in settings is absolute, data/ and models/ follow
   DATA_DIR / MODELS_DIR, and nothing depends on the folder Python started in.
3. Atomic writes — a saved file is complete or untouched, a secret is never
   readable by others, and a failed write leaves no temporary file behind.

Pure Python: runs anywhere.

Run:  PYTHONPATH=edge python tests/test_foundations.py
"""
from __future__ import annotations

import os
import random
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

from common import atomic, device_identity                       # noqa: E402
from config import settings as settings_mod                       # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("\n1. device identity")
code_for = device_identity.code_for
rng = random.Random(7)
serials = {str(rng.randrange(10**12, 10**13)) for _ in range(50_000)}
serials |= {"0001424622063459", "1424622063459", "0", "00", "000"}
codes = {code_for(s)[0] for s in serials}
check(f"{len(serials)} different serials -> {len(codes)} different codes",
      len(codes) == len(serials))
check("leading zeros still give different codes",
      len({code_for(s)[0] for s in ("0", "00", "000")}) == 3)
check("a numeric serial is marked as a guaranteed code",
      code_for("1424622063459")[1] == "serial")
check("a non-numeric serial is hashed and marked so",
      code_for("A1B2-C3")[1] == "serial-hash")
check("the same serial always gives the same code",
      code_for("1424622063459") == code_for("1424622063459"))
ident = device_identity.DeviceIdentity(code=code_for("1424622063459")[0],
                                       serial="1424622063459", source="serial")
check(f"name is a valid hostname ({ident.name})",
      re.fullmatch(r"[a-z0-9-]{1,63}", ident.name) is not None
      and not ident.name.endswith("-"))
check(f"ssid fits Wi-Fi's 32 bytes ({ident.ssid})", len(ident.ssid.encode()) <= 32)
check("no ambiguous letters (I, L, O, U) in any code",
      not any(set(c) & set("ILOU") for c in codes))
check("this machine has an identity", bool(device_identity.identity().code))


print("\n2. paths")
base = Path(tempfile.gettempdir()).resolve()          # absolute on any OS
X, M = base / "srv-x", base / "srv-m"
s = settings_mod.Settings(_env_file=None, data_dir=str(X), models_dir=str(M))
check("data_dir stays absolute", Path(s.data_dir) == X)
check("data/ paths follow DATA_DIR", Path(s.sqlite_path) == X / "ceravis.db"
      and Path(s.record_dir) == X / "recordings")
check("models/ paths follow MODELS_DIR",
      Path(s.detection_model_path) == M / "detection" / "yolo26m.engine")
d = settings_mod.Settings(_env_file=None)
check("by default data lives in edge/data (or DATA_DIR when set)",
      Path(d.data_dir) == settings_mod.resolve_path(os.environ.get("DATA_DIR", "data")))
here = os.getcwd()
os.chdir(tempfile.gettempdir())
try:
    moved = settings_mod.Settings(_env_file=None)
finally:
    os.chdir(here)
check("the start folder changes nothing", moved.sqlite_path == d.sqlite_path)
check("every path setting is absolute",
      all(Path(getattr(d, n)).is_absolute() for n in settings_mod._PATH_FIELDS))
check("an absolute path is kept as given",
      settings_mod.Settings(_env_file=None, sqlite_path=str(base / "db.sqlite")).sqlite_path
      == str(base / "db.sqlite"))
check("device_id defaults to the device name",
      d.device_id == device_identity.identity().name)


print("\n3. atomic writes")
tmp = Path(tempfile.mkdtemp(prefix="ceravis-atomic-"))
target = tmp / "sub" / "state.json"
atomic.write_json_atomic(target, {"a": 1})
check("folder created and file written", target.read_text() == '{\n  "a": 1\n}')
atomic.write_json_atomic(target, {"a": 2}, indent=None)
check("an existing file is replaced whole", target.read_text() == '{"a": 2}')
secret = tmp / "secret.json"
atomic.write_json_atomic(secret, {"k": "v"}, mode=0o600)
if os.name == "posix":
    check("a secret is owner-only (0600)", stat.S_IMODE(secret.stat().st_mode) == 0o600)


class Boom:
    def encode(self, *_):
        raise RuntimeError("disk on fire")


try:
    atomic.write_atomic(target, Boom())       # type: ignore[arg-type]
except Exception:
    pass
check("a failed write leaves the old file intact", target.read_text() == '{"a": 2}')
check("…and no temporary file behind",
      not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")])


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All foundation checks passed.")
