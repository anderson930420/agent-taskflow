"""Atomic file-write helpers for artifact and evidence files.

Evidence gates and validators read artifact files that executors and
reporting commands write. A plain ``Path.write_text(...)`` truncates the
target before writing, so a concurrent reader can observe a partially
written file and fail on malformed JSON.

These helpers write to a temporary file in the same directory, flush and
fsync it, then ``os.replace(...)`` it over the target. Readers therefore
see either the previous complete content or the new complete content,
never a partial write. If writing fails before the replace, the existing
target file is left intact and the temporary file is removed best-effort.

Scope: artifact/evidence/report files only. JSONL append logs, SQLite
writes, and streamed subprocess logs are intentionally not covered.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]


def _fsync_directory_best_effort(directory: Path) -> None:
    """Fsync ``directory`` so the rename itself is durable; ignore failures.

    Directory fsync is not supported on every platform/filesystem, so any
    OSError here is swallowed: atomicity is already guaranteed by
    ``os.replace``, this only strengthens durability after a crash.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Atomically write ``data`` to ``path`` and return the target path.

    The temporary file is created in ``path.parent`` so ``os.replace`` is a
    same-filesystem rename. Parent directories are created if missing.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    _fsync_directory_best_effort(target.parent)
    return target


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    """Atomically write ``text`` to ``path`` and return the target path."""
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    trailing_newline: bool = True,
) -> Path:
    """Atomically write ``payload`` as JSON to ``path``.

    Serialization happens before the temporary file is created, so a
    non-serializable payload never touches the filesystem.
    """
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    return atomic_write_text(path, text)
