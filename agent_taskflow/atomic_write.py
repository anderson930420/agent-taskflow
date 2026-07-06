"""Atomic file-write helpers for artifact and evidence files.

Evidence gates and validators read artifact files that executors and
reporting commands write. A plain ``Path.write_text(...)`` truncates the
target before writing, so a concurrent reader can observe a partially
written file and fail on malformed JSON.

These helpers write to a temporary file in the same directory, flush and
fsync it, then ``os.replace(...)`` it over the target. Readers therefore
see either the previous complete content or the new complete content,
never a partial write. Existing regular-file permission bits are preserved;
new files use normal ``0o666`` creation and process-umask semantics. Symlink
targets are not followed: ``os.replace(...)`` replaces the symlink path itself.

If writing fails before the replace, the existing target file is left intact
and temporary-file cleanup is attempted best-effort. A process crash or
SIGKILL may leave an orphan temporary file behind.

Scope: artifact/evidence/report files only. JSONL append logs, SQLite
writes, and streamed subprocess logs are intentionally not covered.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]


_TEMP_RANDOM_BYTES = 8
_TEMP_CREATE_ATTEMPTS = 100


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


def _existing_regular_file_mode(target: Path) -> int | None:
    """Return target permission bits without following symlinks, if regular."""
    try:
        target_stat = os.stat(target, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(target_stat.st_mode):
        return None
    return stat.S_IMODE(target_stat.st_mode)


def _create_temporary_file(target: Path) -> tuple[int, Path]:
    """Create a unique same-directory temporary file using normal umask rules."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(_TEMP_CREATE_ATTEMPTS):
        random_segment = secrets.token_hex(_TEMP_RANDOM_BYTES)
        tmp_path = target.parent / f".{target.name}.{random_segment}.tmp"
        try:
            return os.open(tmp_path, flags, 0o666), tmp_path
        except FileExistsError:
            continue
    raise FileExistsError(
        f"could not create a unique atomic-write temporary file for {target}"
    )


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Atomically write ``data`` to ``path`` and return the target path.

    The temporary file is created in ``path.parent`` so ``os.replace`` is a
    same-filesystem rename. Parent directories are created if missing. When
    replacing an existing regular file, its permission bits are applied to the
    temporary file before replacement. New files are created from mode
    ``0o666``, subject to the process umask.

    Existing symlinks are not followed; replacement removes the symlink path
    and leaves its former target unchanged. Cleanup on exceptions is
    best-effort, and a crash or SIGKILL may leave an orphan temporary file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    preserved_mode = _existing_regular_file_mode(target)
    fd, tmp_path = _create_temporary_file(target)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            fd = -1
            tmp_file.write(data)
            tmp_file.flush()
            if preserved_mode is not None:
                os.fchmod(tmp_file.fileno(), preserved_mode)
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    _fsync_directory_best_effort(target.parent)
    return target


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    """Write text with the permission semantics of ``atomic_write_bytes``."""
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
    non-serializable payload never touches the filesystem. File permissions,
    symlink replacement, and best-effort orphan cleanup follow
    ``atomic_write_bytes`` semantics.
    """
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    return atomic_write_text(path, text)
