"""
Write a file so that a power cut, a kill or the nightly reboot can never leave it
empty or half-written — the one way the edge saves state files.

The data goes to a temporary sibling (created with its final permissions, so a
secret is never readable by others even for a moment), is flushed to the disk,
then renamed over the real file. A rename is all-or-nothing: a reader sees the
old file or the new one, never a mix.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def write_atomic(path: str | Path, data: bytes | str, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)          # in case the file pre-existed (umask, owner)
        except OSError:
            pass                         # non-POSIX dev box
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_json_atomic(path: str | Path, obj, mode: int = 0o644,
                      indent: int | None = 2) -> None:
    write_atomic(path, json.dumps(obj, indent=indent), mode)
