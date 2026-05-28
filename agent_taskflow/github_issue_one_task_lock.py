"""Shared non-overlap lock for GitHub Issue one-task automation.

This module contains only the advisory lock primitive used by both scheduled
and manual one-task GitHub Issue automation entrypoints. It does not start a
scheduler loop, background worker, daemon, executor, merge, approval, or
cleanup action.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import fcntl


def default_github_issue_one_task_lock_path() -> Path:
    """Return the shared non-overlap lock path for GitHub Issue automation."""

    return (
        Path.home()
        / ".agent-taskflow"
        / "github_issue_one_task_scheduler_tick.lock"
    )


class NonOverlapLock:
    """Small flock-based advisory lock.

    The lock is advisory and process-scoped. If the owning process dies, the
    operating system releases it with the underlying file descriptor.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise ValueError("lock_path must be an absolute path")
        self._handle: Any | None = None

    def acquire(self, *, blocking: bool) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB

        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


__all__ = [
    "NonOverlapLock",
    "default_github_issue_one_task_lock_path",
]
