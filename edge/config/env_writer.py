from __future__ import annotations

"""
Persist a single KEY=value into infra/env/jetson.env at runtime.

Used when the edge learns a value it must keep for the NEXT boot — currently the
`edge_id` the app server hands back at account verification. account.json is the
authoritative runtime source (read live, no restart needed); this writes the same
value into jetson.env so it survives a restart AND so the operator can copy it
into cloud/frpc.toml's `locations`. Best-effort: a write failure is logged, never
raised, so it can't break the verify request.
"""

import logging
from pathlib import Path

logger = logging.getLogger("config")

_ENV_FILE = Path(__file__).resolve().parents[1] / "infra" / "env" / "jetson.env"


def set_env_value(key: str, value: str, env_file: Path | None = None) -> bool:
    """Update (or append) `KEY=value` in the env file, preserving every other
    line. Matches an existing uncommented `KEY=` at the start of a line."""
    path = env_file or _ENV_FILE
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        logger.warning("jetson.env read failed (%s): %s", path, exc)
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
        logger.warning("jetson.env write failed (%s): %s", path, exc)
        return False
    logger.info("jetson.env updated: %s=%s", key, value)
    return True
