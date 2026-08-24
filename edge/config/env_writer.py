from __future__ import annotations

"""
Persist a single KEY=value into infra/env/jetson.env at runtime.

Used when the edge learns a value it must keep for the NEXT boot — currently the
`edge_id` the app server hands back at account verification. account.json is the
authoritative runtime source (read live, no restart needed); this writes the same
value into jetson.env so it survives a restart AND so frpc can be pointed at the
same token. Best-effort: a write failure is logged, never raised, so it can't
break the verify request.

jetson.env is ONE file and it is GITIGNORED — generated from jetson.env.example
by setup/setup.sh. That matters precisely because this module writes to it: a
TRACKED file the device rewrites leaves every unit with a dirty working tree, so
the next commit touching it makes `git pull` abort, and the usual escape
(`git checkout --`) silently discards the edge_id — which is both the fleet
routing token and the control-API credential.
"""

import logging
from pathlib import Path

logger = logging.getLogger("config")

_ENV_FILE = Path(__file__).resolve().parents[1] / "infra" / "env" / "jetson.env"


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
