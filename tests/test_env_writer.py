"""
Runtime env writes must never dirty a TRACKED file.

jetson.env is committed, and env_writer used to write the device's EDGE_ID into
it — so every provisioned device carried a modified tracked file, and any commit
touching jetson.env made `git pull` abort with "local changes would be
overwritten". The usual escape (`git checkout --`) then silently discards the
edge_id, which is the device's routing token AND its control-API credential.

jetson.env has documented "PUT EDGE_ID IN jetson.local.env, NOT HERE" all along;
the writer simply did the opposite.

Run:  PYTHONPATH=edge python tests/test_env_writer.py
"""
import pathlib
import sys
import tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "edge"))
from config import env_writer as ew

tmp = pathlib.Path(tempfile.mkdtemp())
ew._ENV_DIR = tmp
ew._SHARED_FILE = tmp / "jetson.env"
ew._ENV_FILE = tmp / "jetson.local.env"

COMMITTED = "# comment\nEDGE_ID=\nRECORD_SEGMENT_SECS=15\n"

def reset(shared):
    ew._SHARED_FILE.write_text(shared, encoding="utf-8")
    if ew._ENV_FILE.exists(): ew._ENV_FILE.unlink()

ok = True
def chk(label, cond, detail=""):
    global ok
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{('  — '+detail) if detail else ''}")
    ok &= bool(cond)

print("\n1. a runtime write lands in the LOCAL file only")
reset(COMMITTED)
ew.set_env_value("EDGE_ID", "NrPabc123")
chk("local file got the value", "EDGE_ID=NrPabc123" in ew._ENV_FILE.read_text())
chk("tracked file is byte-identical to committed",
    ew._SHARED_FILE.read_text() == COMMITTED)

print("\n2. a device with the key STRANDED in the tracked file is rescued")
reset("# comment\nEDGE_ID=NrPold999\nRECORD_SEGMENT_SECS=15\n")
moved = ew.migrate_to_local()
chk("reported the migration", moved == ["EDGE_ID"], str(moved))
chk("value preserved in local", "EDGE_ID=NrPold999" in ew._ENV_FILE.read_text())
chk("tracked line blanked -> matches committed state",
    ew._SHARED_FILE.read_text() == COMMITTED, repr(ew._SHARED_FILE.read_text()))
chk("other tracked keys untouched",
    "RECORD_SEGMENT_SECS=15" in ew._SHARED_FILE.read_text())

print("\n3. idempotent — a second boot changes nothing")
before_shared, before_local = ew._SHARED_FILE.read_text(), ew._ENV_FILE.read_text()
chk("no-op on re-run", ew.migrate_to_local() == [])
chk("files unchanged", ew._SHARED_FILE.read_text() == before_shared
    and ew._ENV_FILE.read_text() == before_local)

print("\n4. a local value already set is never overwritten by a stale tracked one")
reset("EDGE_ID=NrPstale\n")
ew.set_env_value("EDGE_ID", "NrPcurrent")
ew.migrate_to_local()
chk("local keeps the CURRENT value", "EDGE_ID=NrPcurrent" in ew._ENV_FILE.read_text(),
    ew._ENV_FILE.read_text().strip())
chk("stale tracked value cleared", "NrPstale" not in ew._SHARED_FILE.read_text())

print("\n5. nothing stranded -> nothing to do")
reset(COMMITTED)
chk("clean device is a no-op", ew.migrate_to_local() == [])

print("\nPASS" if ok else "\nFAIL"); sys.exit(0 if ok else 1)
