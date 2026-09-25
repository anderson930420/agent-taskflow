"""The per-repository integration lock: an OS ``flock`` (SPEC §22, §23.1; RULINGS 69).

RULINGS 69 (owner decision D3) makes an OS ``flock`` the sole integration
exclusion authority for a managed repository. The ``integration_locks`` row is
kept only as a journal (see :mod:`agent_taskflow.integration_controller`).

Invariant (RULINGS 69): V1 production is a **single control host**, the lock
directory is on a **local filesystem**, and each managed repository has **one
authoritative integration writer** at a time. ``flock`` gives no exclusion
across hosts or over most network filesystems, so the holder record binds the
lock to one host and one local clone, and acquisition refuses otherwise.

Lock key and files
------------------
The key is the normalized GitHub ``owner/name`` (``Owner/Repo`` and
``owner/repo`` contend). Each key has two files in the lock directory:

* ``<owner>@<name>.lock`` is the ``flock`` target. It is never written,
  renamed or deleted, so its inode stays stable.
* ``<owner>@<name>.json`` is the holder record, replaced atomically and only
  while the ``flock`` is held. It binds the key to a host and a canonical
  ``git-common-dir``. While held it names the holder (pid, start ticks,
  boot_id, host, run_id, task_key, db path, git-common-dir), the process
  groups of the holder's children, and any external holds. A clean release
  clears the holder and keeps the binding.

The lock directory does not depend on the release SHA, the checkout or the
database. It is the explicit ``lock_dir`` argument, else the
``AGENT_TASKFLOW_INTEGRATION_LOCK_DIR`` setting, else
``~/.agent-taskflow/locks/integration``. That default sits under the control
host's live-state root, next to the default ``state.db``, and outlives every
release directory and checkout. It is keyed by repository alone, so two
databases or two checkouts on one host can never both integrate one
repository. It is never derived from the code location.

Children (RULINGS 69, "both together")
--------------------------------------
While :meth:`IntegrationRepoLock.inherited_by_children` is active, integration
git, gh and validator children are started through
:func:`run_integration_child`:

* the ``flock`` descriptor is passed to the child, so a child that outlives a
  killed holder keeps the lock held and no new writer can start beside it;
* the child leads its own session and process group, which is recorded in the
  holder record, so a timeout, and a successor's takeover after a crash,
  can terminate the whole tree. Leftover descendants are terminated when the
  leader exits.

Git background GC is disabled for those git calls by
:mod:`agent_taskflow.integration_git`, so no long-lived git descendant holds
the descriptor.

External holds (OR-8.1)
-----------------------
A process started outside this process tree, such as a validator run as a
``systemd-run`` transient unit (D2, not implemented here), does not inherit
the descriptor. Whoever starts one records it with
:meth:`IntegrationRepoLock.record_external_hold`. A successor takes over only
when an :class:`ExternalHoldProbe` proves every recorded hold gone. The
default probe proves nothing, so any recorded hold blocks takeover until D2
supplies a real probe.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
from typing import Any, Iterator, Mapping, Protocol, Sequence

from agent_taskflow.atomic_write import atomic_write_json
# The managed-process primitives of executor_launch, reused so integration
# children and executor children share one proof of process-group exit.
from agent_taskflow.executor_launch import (
    _read_proc_stat,
    _wait_group_exit,
    inspect_process_group,
)
from agent_taskflow.integration_schema import normalize_repo
from agent_taskflow.models import utc_now_iso


__all__ = [
    "DEFAULT_LOCK_DIR_PARTS",
    "ExternalHoldProbe",
    "FailClosedExternalHoldProbe",
    "IntegrationRepoLock",
    "IntegrationRepoLockError",
    "LOCK_DIR_ENV",
    "LockAcquisition",
    "SCHEMA_VERSION",
    "active_integration_lock",
    "lock_key",
    "read_boot_id",
    "resolve_lock_dir",
    "run_integration_child",
]


SCHEMA_VERSION = "integration_repo_lock.v1"
LOCK_DIR_ENV = "AGENT_TASKFLOW_INTEGRATION_LOCK_DIR"
DEFAULT_LOCK_DIR_PARTS = (".agent-taskflow", "locks", "integration")

# A holder record is a small JSON document; anything much larger is not one.
_MAX_RECORD_BYTES = 256 * 1024
_TERMINATE_GRACE_SECONDS = 2.0
_KILL_WAIT_SECONDS = 3.0
# Popen options run_integration_child owns and callers may not override.
_RESERVED_POPEN_OPTIONS = frozenset(
    {"start_new_session", "process_group", "pass_fds", "preexec_fn", "close_fds"}
)

_ACTIVE_LOCK: ContextVar["IntegrationRepoLock | None"] = ContextVar(
    "agent_taskflow_active_integration_lock", default=None
)


class IntegrationRepoLockError(RuntimeError):
    """The lock was refused: acquiring it could break the single-writer invariant.

    Unlike a lock that is merely held, a refusal does not clear by waiting. It
    needs a human, and the files are left untouched for them.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExternalHoldProbe(Protocol):
    """Proves that a recorded external hold (a unit, a cgroup) is gone."""

    def is_gone(self, hold: Mapping[str, Any]) -> bool:
        ...


class FailClosedExternalHoldProbe:
    """The default probe: it can prove nothing gone, so every hold blocks takeover."""

    def is_gone(self, hold: Mapping[str, Any]) -> bool:
        return False


@dataclass(frozen=True)
class LockAcquisition:
    """The outcome of one :meth:`IntegrationRepoLock.acquire`."""

    acquired: bool
    # "acquired", "held" or "external_hold_active".
    reason: str
    detail: str = ""
    # The holder a dead process left in the record, found on acquire.
    previous_holder: dict[str, Any] | None = None
    # The recorded holder when the lock is held by someone else.
    active_holder: dict[str, Any] | None = None
    # Orphaned child process groups this acquire terminated.
    terminated_children: tuple[dict[str, Any], ...] = ()
    # External holds proven gone, or still blocking.
    external_holds: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "reason": self.reason,
            "detail": self.detail,
            "previous_holder": self.previous_holder,
            "active_holder": self.active_holder,
            "terminated_children": [dict(item) for item in self.terminated_children],
            "external_holds": [dict(item) for item in self.external_holds],
        }


def lock_key(repo: str) -> str:
    """Return the lock key: the normalized GitHub ``owner/name``."""
    return normalize_repo(repo)


def resolve_lock_dir(explicit: str | Path | None = None) -> Path:
    """Return the lock directory: explicit, else the setting, else the default."""
    if explicit is not None:
        value = Path(explicit)
    elif os.environ.get(LOCK_DIR_ENV, "").strip():
        value = Path(os.environ[LOCK_DIR_ENV].strip())
    else:
        value = Path.home().joinpath(*DEFAULT_LOCK_DIR_PARTS)
    if not value.is_absolute():
        raise ValueError(f"integration lock_dir must be an absolute path, got {value}")
    return value


def read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def active_integration_lock() -> "IntegrationRepoLock | None":
    """The lock whose descriptor integration children inherit right now, if any."""
    return _ACTIVE_LOCK.get()


def run_integration_child(
    argv: Sequence[str], *, timeout: float | None = None, check: bool = False, **kwargs: Any
) -> subprocess.CompletedProcess:
    """Run one integration child: managed under the active lock, else as before.

    A drop-in for ``subprocess.run`` for the integration git, gh and validator
    runners. With no active lock (cleanup, the watcher, a dry run) it is
    exactly ``subprocess.run``.
    """
    lock = _ACTIVE_LOCK.get()
    if lock is None:
        return subprocess.run(list(argv), timeout=timeout, check=check, **kwargs)
    return lock.run_child(argv, timeout=timeout, check=check, **kwargs)


def _key_file_stem(key: str) -> str:
    # ``@`` cannot occur in an ``owner/name`` key, so the name is unambiguous.
    owner, name = key.split("/", 1)
    return f"{owner}@{name}"


def _process_alive(pid: Any, start_ticks: Any, boot_id: Any) -> bool:
    """True only for the very process recorded: same boot, pid and start time."""
    if not isinstance(pid, int) or boot_id != read_boot_id():
        return False
    stat = _read_proc_stat(pid)
    if stat is None or not stat.live:
        return False
    return start_ticks is None or stat.start_ticks == start_ticks


def _child_group_alive(child: Mapping[str, Any]) -> bool:
    """True while any member of a recorded child's process group is alive."""
    pgid, session_id = child.get("pgid"), child.get("session_id")
    if not isinstance(pgid, int) or not isinstance(session_id, int):
        return False
    if child.get("boot_id") != read_boot_id():
        return False
    leader = _read_proc_stat(pgid)
    if (
        leader is not None
        and leader.live
        and child.get("start_ticks") is not None
        and leader.start_ticks != child.get("start_ticks")
    ):
        # The pid now names another process, so the recorded group is gone:
        # Linux never reuses a pid while it is still a live group's id.
        return False
    return not inspect_process_group(pgid, session_id).verified_exited


def _terminate_child_group(child: Mapping[str, Any]) -> bool:
    """SIGTERM, then SIGKILL, one recorded child group; True once it is gone."""
    if not _child_group_alive(child):
        return True
    pgid, session_id = int(child["pgid"]), int(child["session_id"])
    for signum, wait in (
        (signal.SIGTERM, _TERMINATE_GRACE_SECONDS),
        (signal.SIGKILL, _KILL_WAIT_SECONDS),
    ):
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            return True
        if _wait_group_exit(pgid, session_id, wait):
            return True
    return not _child_group_alive(child)


def _require_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_hold(value: Any) -> bool:
    return isinstance(value, dict) and _require_str(value.get("kind")) and _require_str(
        value.get("identifier")
    )


def _valid_child(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(name), int) for name in ("pid", "pgid", "session_id")
    )


def _valid_holder(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("pid"), int)
        and all(
            _require_str(value.get(name))
            for name in ("host", "run_id", "task_key", "db_path", "git_common_dir")
        )
        and isinstance(value.get("children"), list)
        and all(_valid_child(child) for child in value["children"])
    )


class IntegrationRepoLock:
    """The ``flock`` for one managed repository, with its holder record.

    One object is one holder: acquire, run children, release. It is not
    reentrant and not shared between threads.
    """

    def __init__(
        self,
        repo: str,
        *,
        git_common_dir: str | Path,
        run_id: str,
        task_key: str,
        db_path: str | Path,
        owner: str = "integration_controller",
        lock_dir: str | Path | None = None,
        external_hold_probe: ExternalHoldProbe | None = None,
        terminate_orphaned_children: bool = True,
    ) -> None:
        self.key = lock_key(repo)
        common = Path(git_common_dir)
        if not common.is_absolute():
            raise ValueError("git_common_dir must be an absolute path")
        self.git_common_dir = str(common.resolve())
        self.lock_dir = resolve_lock_dir(lock_dir)
        stem = _key_file_stem(self.key)
        self.lock_path = self.lock_dir / f"{stem}.lock"
        self.record_path = self.lock_dir / f"{stem}.json"
        self.host = socket.gethostname()
        self._holder_fields = {
            "run_id": run_id,
            "task_key": task_key,
            "db_path": str(db_path),
            "owner": owner,
        }
        self._probe = external_hold_probe or FailClosedExternalHoldProbe()
        self._terminate_orphans = terminate_orphaned_children
        self._fd: int | None = None
        self._record: dict[str, Any] | None = None

    # -- state -----------------------------------------------------------
    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def fd(self) -> int | None:
        return self._fd

    def read_record(self) -> dict[str, Any] | None:
        """Read and validate the holder record; None when there is none yet."""
        try:
            descriptor = os.open(self.record_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise IntegrationRepoLockError(
                    "holder_record_symlink",
                    f"Integration lock record {self.record_path} is a symlink; refused",
                ) from exc
            raise
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        return self._validate_record(raw)

    def _validate_record(self, raw: bytes) -> dict[str, Any]:
        def corrupt(why: str) -> IntegrationRepoLockError:
            return IntegrationRepoLockError(
                "corrupt_holder_record",
                f"Integration lock record {self.record_path} is not a valid holder "
                f"record ({why}); it is left untouched and the lock is refused",
            )

        if len(raw) > _MAX_RECORD_BYTES:
            raise corrupt("too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except ValueError as exc:  # includes UnicodeDecodeError
            raise corrupt("not JSON") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise corrupt("unknown schema")
        if value.get("repo") != self.key:
            raise corrupt(f"it names repository {value.get('repo')!r}")
        if not _require_str(value.get("host")) or not _require_str(value.get("git_common_dir")):
            raise corrupt("no host or git_common_dir binding")
        holder = value.get("holder")
        if holder is not None and not _valid_holder(holder):
            raise corrupt("malformed holder")
        holds = value.get("external_holds")
        if not isinstance(holds, list) or not all(_valid_hold(hold) for hold in holds):
            raise corrupt("malformed external_holds")
        return value

    def _check_binding(self, record: Mapping[str, Any] | None) -> None:
        if record is None:
            return
        if record["host"] != self.host:
            raise IntegrationRepoLockError(
                "host_mismatch",
                f"Integration lock {self.key} is bound to host {record['host']!r}, not "
                f"{self.host!r}. The lock requires a single control host with a local "
                f"lock directory; refused. Record: {self.record_path}",
            )
        if record["git_common_dir"] != self.git_common_dir:
            raise IntegrationRepoLockError(
                "git_common_dir_mismatch",
                f"Integration lock {self.key} is bound to the clone at "
                f"{record['git_common_dir']!r}, not {self.git_common_dir!r}. One "
                "repository has one authoritative integration writer; refused. "
                f"Record: {self.record_path}",
            )

    def _write_record(self) -> None:
        assert self._record is not None
        atomic_write_json(self.record_path, self._record, sort_keys=True)

    # -- acquire / release ------------------------------------------------
    def _open_lock_file(self) -> int:
        self.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            return os.open(
                self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise IntegrationRepoLockError(
                    "lock_file_symlink", f"Integration lock {self.lock_path} is a symlink; refused"
                ) from exc
            raise

    @staticmethod
    def _try_flock(descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        return True

    def _terminate_orphans_of(
        self, holder: Mapping[str, Any] | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Stop a dead holder's live child groups; return (terminated, survivors).

        A live holder's children are never touched.
        """
        if holder is None or _process_alive(
            holder.get("pid"), holder.get("start_ticks"), holder.get("boot_id")
        ):
            return [], []
        terminated, survivors = [], []
        for child in holder.get("children", []):
            if not _child_group_alive(child):
                continue
            if self._terminate_orphans and _terminate_child_group(child):
                terminated.append(dict(child))
            else:
                survivors.append(dict(child))
        return terminated, survivors

    def acquire(self) -> LockAcquisition:
        """Take the lock without waiting.

        Returns ``acquired=False`` while another holder, or a child that
        outlived one, holds the ``flock``, or while a recorded external hold is
        not proven gone. Raises :class:`IntegrationRepoLockError`, having
        changed nothing, on a corrupt record or a host or clone mismatch.
        """
        if self._fd is not None:
            raise RuntimeError("the integration lock is already held by this object")
        # Refuse a mismatch before touching anything, even while it is held.
        self._check_binding(self.read_record())
        descriptor = self._open_lock_file()
        try:
            return self._acquire_with(descriptor)
        except BaseException:
            if self._fd is None:
                os.close(descriptor)  # closing also drops a flock taken above
            raise

    def _acquire_with(self, descriptor: int) -> LockAcquisition:
        terminated: list[dict[str, Any]] = []
        if not self._try_flock(descriptor):
            # Held. If its recorded holder is dead, the holders are orphaned
            # children that inherited the descriptor: terminate their groups
            # and try once more. A live holder is never touched.
            record = self.read_record()
            holder = None if record is None else record.get("holder")
            if holder is not None:
                terminated, _survivors = self._terminate_orphans_of(holder)
            if not terminated or not self._try_flock(descriptor):
                os.close(descriptor)
                return LockAcquisition(
                    acquired=False,
                    reason="held",
                    detail=(
                        f"Integration lock for {self.key} is held"
                        + (f" by run {holder.get('run_id')} pid {holder.get('pid')}"
                           if holder else "")
                    ),
                    active_holder=holder,
                    terminated_children=tuple(terminated),
                )

        # Held by us now. A lock file replaced under a holder would let two
        # writers each hold "the" lock, so the path must still be our inode.
        opened, current = os.fstat(descriptor), os.stat(self.lock_path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise IntegrationRepoLockError(
                "lock_file_replaced",
                f"Integration lock {self.lock_path} was replaced while opening it; refused",
            )
        record = self.read_record()
        self._check_binding(record)
        previous = None if record is None else record.get("holder")
        if previous is not None and _process_alive(
            previous.get("pid"), previous.get("start_ticks"), previous.get("boot_id")
        ):
            raise IntegrationRepoLockError(
                "live_holder_without_flock",
                f"Integration lock {self.key} records live holder pid "
                f"{previous.get('pid')} but its flock was free; refused",
            )
        # A dead holder's children that closed the descriptor are still ours
        # to stop before anyone writes again.
        stopped, survivors = self._terminate_orphans_of(previous)
        terminated.extend(stopped)
        if survivors:
            os.close(descriptor)
            return LockAcquisition(
                acquired=False,
                reason="held",
                detail=f"Children of dead holder run {previous.get('run_id')} are still alive",
                previous_holder=previous,
                active_holder=previous,
                terminated_children=tuple(terminated),
            )
        holds = [] if record is None else [dict(hold) for hold in record["external_holds"]]
        blocking = [hold for hold in holds if not self._probe.is_gone(hold)]
        if blocking:
            os.close(descriptor)
            return LockAcquisition(
                acquired=False,
                reason="external_hold_active",
                detail=(
                    f"Integration lock for {self.key}: {len(blocking)} recorded external "
                    "hold(s) are not proven gone"
                ),
                previous_holder=previous,
                terminated_children=tuple(terminated),
                external_holds=tuple(blocking),
            )

        stat = _read_proc_stat(os.getpid())
        self._record = {
            "schema_version": SCHEMA_VERSION,
            "repo": self.key,
            "host": self.host,
            "git_common_dir": self.git_common_dir,
            "holder": {
                **self._holder_fields,
                "pid": os.getpid(),
                "start_ticks": None if stat is None else stat.start_ticks,
                "boot_id": read_boot_id(),
                "host": self.host,
                "git_common_dir": self.git_common_dir,
                "acquired_at": utc_now_iso(),
                "children": [],
            },
            "external_holds": [],
            "released_at": None,
        }
        self._fd = descriptor
        self._write_record()
        return LockAcquisition(
            acquired=True,
            reason="acquired",
            previous_holder=previous,
            terminated_children=tuple(terminated),
            external_holds=tuple(holds),
        )

    def release(self) -> None:
        """Clear the holder, keep the binding and any external holds, unlock."""
        if self._fd is None:
            return
        descriptor, self._fd = self._fd, None
        try:
            if self._record is not None:
                self._record["holder"] = None
                self._record["released_at"] = utc_now_iso()
                self._write_record()
        finally:
            try:
                # LOCK_UN releases the open file description, so a leftover
                # descendant that still has the descriptor no longer holds it.
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                self._record = None

    # -- external holds (OR-8.1) -------------------------------------------
    def record_external_hold(self, kind: str, identifier: str, **detail: Any) -> dict[str, Any]:
        """Record, before starting it, a process that will not inherit the flock."""
        if self._fd is None or self._record is None:
            raise RuntimeError("an external hold can only be recorded while holding the lock")
        if not _require_str(kind) or not _require_str(identifier):
            raise ValueError("an external hold needs a nonempty kind and identifier")
        hold = {**detail, "kind": kind, "identifier": identifier, "recorded_at": utc_now_iso()}
        self._record["external_holds"].append(hold)
        self._write_record()
        return hold

    def clear_external_hold(self, kind: str, identifier: str) -> bool:
        """Drop a hold only once the probe proves it gone; True when dropped."""
        if self._fd is None or self._record is None:
            raise RuntimeError("an external hold can only be cleared while holding the lock")
        holds = self._record["external_holds"]
        matching = [h for h in holds if h["kind"] == kind and h["identifier"] == identifier]
        if not matching or not all(self._probe.is_gone(hold) for hold in matching):
            return False
        self._record["external_holds"] = [hold for hold in holds if hold not in matching]
        self._write_record()
        return True

    # -- children ---------------------------------------------------------
    @contextmanager
    def inherited_by_children(self) -> Iterator["IntegrationRepoLock"]:
        """Route integration children through :meth:`run_child` for this block."""
        if self._fd is None:
            raise RuntimeError("children can only inherit a lock that is held")
        token = _ACTIVE_LOCK.set(self)
        try:
            yield self
        finally:
            _ACTIVE_LOCK.reset(token)

    def _set_children(self, children: list[dict[str, Any]]) -> None:
        assert self._record is not None and self._record["holder"] is not None
        self._record["holder"]["children"] = children
        self._write_record()

    def run_child(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = False,
        input: Any = None,
        **popen_kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """Run one child in its own process group, holding the flock descriptor."""
        if self._fd is None or self._record is None:
            raise RuntimeError("the integration lock is not held")
        reserved = sorted(_RESERVED_POPEN_OPTIONS & set(popen_kwargs))
        if reserved:
            raise ValueError(f"run_child owns these Popen options: {', '.join(reserved)}")
        if input is not None:
            popen_kwargs["stdin"] = subprocess.PIPE
        process = subprocess.Popen(
            list(argv), start_new_session=True, pass_fds=(self._fd,), **popen_kwargs
        )
        stat = _read_proc_stat(process.pid)
        child = {
            "pid": process.pid,
            "pgid": process.pid,
            "session_id": process.pid,
            "start_ticks": None if stat is None else stat.start_ticks,
            "boot_id": read_boot_id(),
            "argv0": str(list(argv)[0]) if argv else "",
            "started_at": utc_now_iso(),
        }
        children = list(self._record["holder"]["children"])
        try:
            self._set_children([*children, child])
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_child_group(child)
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(
                    process.args, timeout, output=stdout, stderr=stderr
                ) from None
            except BaseException:
                _terminate_child_group(child)
                process.kill()
                process.wait()
                raise
            # The leader exited; nothing it left behind may keep the lock.
            _terminate_child_group(child)
        finally:
            if self._record is not None and self._record.get("holder") is not None:
                self._set_children(children)
        completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed
