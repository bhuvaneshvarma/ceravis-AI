from __future__ import annotations

"""
Persist a single KEY=value the device learned at runtime — into the DEVICE-LOCAL
env file, never the committed one.

Used when the edge learns a value it must keep for the NEXT boot — currently the
`edge_id` the app server hands back at account verification. account.json is the
authoritative runtime source (read live, no restart needed); this writes the same
value into the env file so it survives a restart AND so the operator can copy it
into cloud/frpc.toml's `locations`. Best-effort: a write failure is logged, never
raised, so it can't break the verify request.

WHY jetson.local.env AND NOT jetson.env: jetson.env is TRACKED IN GIT. Writing a
per-device value into a tracked file leaves every device with a dirty working
tree, so the next commit that touches that file makes `git pull` abort with
"local changes would be overwritten" — and the usual escape (`git checkout --`)
silently discards the device's edge_id, which is its routing token and its
control-API credential.

jetson.env has said "PUT EDGE_ID IN jetson.local.env, NOT HERE" since it was
written; this module simply did the opposite. The local file is gitignored and
is loaded LAST by pydantic-settings, so a value here wins over the committed
default and survives every pull untouched.
"""

import logging
from pathlib import Path

logger = logging.getLogger("config")

_ENV_DIR = Path(__file__).resolve().parents[1] / "infra" / "env"
_SHARED_FILE = _ENV_DIR / "jetson.env"          # committed; never written here
_ENV_FILE = _ENV_DIR / "jetson.local.env"       # gitignored; runtime writes land here


def set_env_value(key: str, value: str, env_file: Path | None = None) -> bool:
    """Update (or append) `KEY=value` in the env file, preserving every other
    line. Matches an existing uncommented `KEY=` at the start of a line."""
    path = env_file or _ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        logger.warning("env read failed (%s): %s", path, exc)
        return False

    prefix = f"{key}="
    new_line = f"{key}={value}"
    out: list[str] = []
    replaced = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(prefix) and not stripped.startswith("#"):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)

    try:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("env write failed (%s): %s", path, exc)
        return False
    logger.info("%s updated: %s=%s", path.name, key, value)
    return True


def migrate_to_local(keys: tuple[str, ...] = ("EDGE_ID",)) -> list[str]:
    """Move runtime-written keys out of the TRACKED jetson.env into the local
    override. Idempotent, best-effort, and safe to run on every boot.

    Devices provisioned before this module was corrected have their edge_id in
    jetson.env, which is exactly what makes `git pull` abort. Copying it into
    jetson.local.env (which wins on load) and blanking the tracked line restores
    that file to its committed state, so the next pull is clean and the device
    keeps its token.
    """
    moved: list[str] = []
    try:
        shared = _SHARED_FILE.read_text(encoding="utf-8") if _SHARED_FILE.exists() else ""
        local = _ENV_FILE.read_text(encoding="utf-8") if _ENV_FILE.exists() else ""
    except OSError as exc:
        logger.warning("env migration skipped: %s", exc)
        return moved

    for key in keys:
        prefix = f"{key}="
        value = ""
        for line in shared.splitlines():
            st = line.lstrip()
            if st.startswith(prefix) and not st.startswith("#"):
                value = st[len(prefix):].strip()
        if not value:
            continue                       # nothing stranded in the tracked file
        already = any(l.lstrip().startswith(prefix) and not l.lstrip().startswith("#")
                      and l.split("=", 1)[1].strip()
                      for l in local.splitlines())
        if not already and not set_env_value(key, value):
            continue                       # local write failed — leave shared alone
        # Blank the tracked line so jetson.env matches its committed state again.
        if set_env_value(key, "", env_file=_SHARED_FILE):
            moved.append(key)
            logger.info("migrated %s out of the tracked jetson.env into "
                        "jetson.local.env (pull-safe from now on)", key)
    return moved
