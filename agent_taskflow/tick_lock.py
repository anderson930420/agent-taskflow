"""Per-cron-entry non-overlap locks for the V1 ticks (SPEC §47.3; V1-F10).

Each tick entry point holds its own advisory ``flock`` for its whole run. A
second invocation that finds the lock held exits at once with a logged,
machine-readable ``skipped_overlap`` result and does no work: no queueing, no
pile-up, and never a silent skip.

These locks are separate from the §23.1 per-repository integration lock, which
is a database row owned by ``integrate_task``. They protect one tick script
from overlapping itself; they never serialize integration work.

Lock files are derived only from the explicit ``--db-path`` (or an explicit
absolute ``--lock-path``), beside the resolved database file. There is no
default under ``~/.agent-taskflow``. Because the key is the resolved database
path, a hand-run and a cron-run invocation of the same tick contend on the
same lock:

* execution tick:   ``<db>.execution-tick.lock``
* integration tick: ``<db>.integration-tick.<owner>@<name>.lock`` — keyed by
  ``(db_path, repo)``, so different repositories integrate concurrently (§22).

``flock`` is released by the kernel when the holding process dies, including
by SIGKILL. The lock file is opened close-on-exec, so no git, gh, validator or
worker child inherits it.

A lock file is only ever empty or one holder record, so a lock never
overwrites anything else. It refuses a path that is the database or one of
its ``-wal``, ``-shm`` or ``-journal`` files, a symlink, and an existing file
with any other content (review N2).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from agent_taskflow.integration_schema import normalize_repo
from agent_taskflow.models import utc_now_iso


__all__ = [
    "DATABASE_SIDE_FILES",
    "EXIT_SKIPPED_OVERLAP",
    "SKIPPED_OVERLAP",
    "TickLock",
    "TickLockPathError",
    "execution_tick_lock_path",
    "integration_tick_lock_path",
    "skipped_overlap_result",
]


# sysexits.h EX_TEMPFAIL: "temporary failure; the user is invited to retry".
# Distinct from every other tick exit code (0 ok, 1 not ok, 2 error).
EXIT_SKIPPED_OVERLAP = 75
SKIPPED_OVERLAP = "skipped_overlap"

# SQLite keeps these beside the database; a lock path must never name one.
DATABASE_SIDE_FILES = ("", "-wal", "-shm", "-journal")
# A holder record is one short JSON line; anything longer is not one.
_MAX_RECORD_BYTES = 4096


class TickLockPathError(ValueError):
    """The lock path names a file that is not, and may not become, a tick lock."""


def _resolved_db_path(db_path: str | Path) -> Path:
    path = Path(db_path)
    if not path.is_absolute():
        raise ValueError("db_path must be an absolute path")
    return path.resolve()


def execution_tick_lock_path(db_path: str | Path) -> Path:
    """Return the execution tick's lock path, keyed by the database."""
    db = _resolved_db_path(db_path)
    return db.with_name(f"{db.name}.execution-tick.lock")


def integration_tick_lock_path(db_path: str | Path, repo: str) -> Path:
    """Return the integration tick's lock path, keyed by database and repository.

    The repository is normalized, so ``Owner/Repo`` and ``owner/repo`` contend.
    ``@`` cannot occur in an ``owner/name`` key, so the name is unambiguous.
    """
    db = _resolved_db_path(db_path)
    owner, name = normalize_repo(repo).split("/", 1)
    return db.with_name(f"{db.name}.integration-tick.{owner}@{name}.lock")


def _database_files(db_path: str | Path) -> frozenset[Path]:
    """The database and its SQLite side files, as named and as resolved."""
    db = Path(db_path)
    if not db.is_absolute():
        raise ValueError("db_path must be an absolute path")
    return frozenset(
        base.with_name(base.name + suffix).resolve()
        for base in (db, db.resolve())
        for suffix in DATABASE_SIDE_FILES
    )


def _holder_record(raw: bytes) -> dict[str, Any] | None:
    """Parse one holder record as :meth:`TickLock.acquire` writes it, or None."""
    if len(raw) > _MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        return None
    if (
        isinstance(value, dict)
        and isinstance(value.get("pid"), int)
        and isinstance(value.get("acquired_at"), str)
    ):
        return value
    return None


class TickLock:
    """A non-blocking, process-scoped ``flock`` with a recorded holder.

    Unlike ``NonOverlapLock`` it never creates directories and never blocks.
    While held, the file records who holds it, so a skipped invocation can
    report the holder; the record is cleared on a normal release. A record
    left by a holder that was killed is replaced by the next holder.
    """

    def __init__(
        self, path: str | Path, *, holder: dict[str, Any], db_path: str | Path
    ) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("lock_path must be an absolute path")
        self._database_files = _database_files(db_path)
        if self.path.resolve() in self._database_files:
            raise TickLockPathError(
                f"lock_path {self.path} is the database {db_path} or one of its "
                "-wal, -shm or -journal files; refused"
            )
        self._holder = dict(holder)
        self._handle: Any | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Take the lock without waiting. Returns False if another holds it.

        Raises :class:`TickLockPathError`, having written nothing, when the path
        is a symlink, is the database under another name, or holds anything
        but a holder record.
        """
        if self._handle is not None:
            raise RuntimeError("lock is already held by this object")
        try:
            descriptor = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise TickLockPathError(f"lock_path {self.path} is a symlink; refused") from exc
            raise
        handle = os.fdopen(descriptor, "r+b")
        try:
            # Also catches a hard link to, or a rename of, a database file.
            opened = os.fstat(handle.fileno())
            for name in self._database_files:
                try:
                    other = os.stat(name)
                except OSError:
                    continue
                if (opened.st_dev, opened.st_ino) == (other.st_dev, other.st_ino):
                    raise TickLockPathError(
                        f"lock_path {self.path} is the database file {name}; refused"
                    )
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    handle.close()
                    return False
                raise
            existing = handle.read(_MAX_RECORD_BYTES + 1)
            if existing.strip() and _holder_record(existing) is None:
                raise TickLockPathError(
                    f"lock_path {self.path} holds something other than a tick holder "
                    "record and is left untouched; refused"
                )
            record = {**self._holder, "pid": os.getpid(), "acquired_at": utc_now_iso()}
            handle.seek(0)
            handle.truncate()
            handle.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
        except BaseException:
            handle.close()  # closing the descriptor also drops the flock
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def recorded_holder(self) -> dict[str, Any] | None:
        """Best-effort read of the current holder's record, never raising.

        None means no Taskflow tick has recorded itself: the holder is another
        process (for example ``flock(1)``) or is still writing its record.
        """
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_RECORD_BYTES + 1)
        except OSError:
            return None
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return {"unparsed": text[:200]}
        return value if isinstance(value, dict) else {"unparsed": text[:200]}


def skipped_overlap_result(kind: str, lock: TickLock, **fields: Any) -> dict[str, Any]:
    """The logged, machine-readable result of a skipped invocation (§47.3)."""
    return {
        "kind": kind,
        "ok": False,
        "status": SKIPPED_OVERLAP,
        "exit_code": EXIT_SKIPPED_OVERLAP,
        "reason": "another invocation of this tick holds its non-overlap lock",
        "work_performed": False,
        "lock_path": str(lock.path),
        "holder": lock.recorded_holder(),
        **fields,
        "generated_at": utc_now_iso(),
    }
