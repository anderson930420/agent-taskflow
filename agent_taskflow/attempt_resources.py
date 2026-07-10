"""Attempt-scoped branch, worktree, lock, PID, and artifact resources."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Any

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.attempt_resources_schema import migrate_attempt_resources
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord, require_absolute_path, utc_now_iso
from agent_taskflow.runtime_admission import RuntimeClaim
from agent_taskflow.store import TaskMirrorStore, connect, default_db_path
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.workspace_manager import (
    WORKSPACE_BLOCKED,
    WORKSPACE_PREPARED,
    WORKSPACE_REUSED,
    WorkspacePreparationResult,
)

ATTEMPT_RESOURCE_MANIFEST = "attempt-resources.json"
ATTEMPT_PID_FILENAME = "runtime.pid.json"
ATTEMPT_LOCK_FILENAME = "runtime.lock"
ATTEMPT_INPUT_FILENAMES = (
    "issue_spec.md",
    "implementation_prompt.md",
    "codex-advisory-review.json",
    "codex-advisory-review.md",
    "codex-advisory-review-prompt.md",
    "codex-advisory-review-stdout.txt",
    "codex-advisory-review-stderr.txt",
)


class AttemptResourceError(RuntimeError):
    """Raised when Attempt-scoped resources cannot be allocated safely."""


@dataclass(frozen=True)
class AttemptResourceRecord:
    attempt_id: str
    task_id: str
    task_key: str
    attempt_number: int
    owner_id: str
    repo_path: Path
    base_branch: str
    base_sha: str | None
    branch_name: str
    worktree_root: Path
    worktree_path: Path
    artifact_base_root: Path
    artifact_root: Path
    lock_path: Path
    pid_path: Path
    runtime_pid: int | None
    status: str
    allocated_at: str
    activated_at: str | None
    released_at: str | None
    reaped_at: str | None
    updated_at: str
    release_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "repo_path",
            "worktree_root",
            "worktree_path",
            "artifact_base_root",
            "artifact_root",
            "lock_path",
            "pid_path",
        ):
            payload[key] = str(payload[key])
        return payload


@dataclass
class AttemptResourceHandle:
    record: AttemptResourceRecord
    lock: "AttemptFileLock"


class AttemptFileLock:
    """Attempt-specific flock retained for the lifetime of one runtime process."""

    def __init__(self, path: str | Path) -> None:
        self.path = require_absolute_path(path, "lock_path")
        self._handle: Any | None = None

    def acquire(self, *, blocking: bool, metadata: dict[str, Any]) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
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


def _row_to_record(row: sqlite3.Row) -> AttemptResourceRecord:
    return AttemptResourceRecord(
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        task_key=row["task_key"],
        attempt_number=int(row["attempt_number"]),
        owner_id=row["owner_id"],
        repo_path=Path(row["repo_path"]),
        base_branch=row["base_branch"],
        base_sha=row["base_sha"],
        branch_name=row["branch_name"],
        worktree_root=Path(row["worktree_root"]),
        worktree_path=Path(row["worktree_path"]),
        artifact_base_root=Path(row["artifact_base_root"]),
        artifact_root=Path(row["artifact_root"]),
        lock_path=Path(row["lock_path"]),
        pid_path=Path(row["pid_path"]),
        runtime_pid=row["runtime_pid"],
        status=row["status"],
        allocated_at=row["allocated_at"],
        activated_at=row["activated_at"],
        released_at=row["released_at"],
        reaped_at=row["reaped_at"],
        updated_at=row["updated_at"],
        release_reason=row["release_reason"],
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")
    return normalized or "task"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_message(result: subprocess.CompletedProcess[bytes]) -> str:
    return (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()


def _worktrees(repo_path: Path) -> list[dict[str, str]]:
    result = _git(["worktree", "list", "--porcelain"], repo_path)
    if result.returncode != 0:
        raise AttemptResourceError(f"could not list git worktrees: {_git_message(result)}")
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


class AttemptResourceManager:
    """Allocate immutable paths and retain historical resources per Attempt."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = default_db_path() if db_path is None else require_absolute_path(db_path, "db_path")

    def init_db(self) -> None:
        migrate_attempt_resources(self.db_path)

    def get(self, attempt_id: str) -> AttemptResourceRecord | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM attempt_resources WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def latest_for_task(self, task_key: str) -> AttemptResourceRecord | None:
        self.init_db()
        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT attempt_resources.*
                FROM attempt_resources
                WHERE task_key = ?
                ORDER BY attempt_number DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    @staticmethod
    def _safe_worktree_root(repo_path: Path, requested: Path | None) -> Path:
        canonical = (repo_path / ".worktrees").resolve()
        root = (requested or canonical).resolve()
        try:
            root.relative_to(canonical)
        except ValueError as exc:
            raise AttemptResourceError(
                f"worktree_root must be inside {canonical}: {root}"
            ) from exc
        return root

    def allocate(
        self,
        claim: RuntimeClaim,
        task: TaskRecord,
        *,
        base_branch: str = "main",
        worktree_root: str | Path | None = None,
        artifact_base_root: str | Path | None = None,
    ) -> AttemptResourceHandle:
        """Allocate unique immutable paths, lock, PID, and manifest for one Attempt."""
        self.init_db()
        repo_path = require_absolute_path(task.repo_path, "repo_path")
        latest = self.latest_for_task(task.task_key)
        requested_worktree = (
            require_absolute_path(worktree_root, "worktree_root")
            if worktree_root is not None
            else (latest.worktree_root if latest is not None else None)
        )
        resolved_worktree_root = self._safe_worktree_root(repo_path, requested_worktree)
        if artifact_base_root is not None:
            resolved_artifact_base = require_absolute_path(
                artifact_base_root, "artifact_base_root"
            )
        elif latest is not None:
            resolved_artifact_base = latest.artifact_base_root
        elif task.artifact_dir is not None:
            resolved_artifact_base = require_absolute_path(
                task.artifact_dir, "artifact_base_root"
            )
        else:
            raise AttemptResourceError("Task artifact_dir is required for Attempt resources")

        normalized_branch = base_branch.strip()
        if not normalized_branch:
            raise AttemptResourceError("base_branch must not be empty")
        task_slug = _slug(task.task_key)
        attempt_suffix = claim.attempt_id.removeprefix("attempt-")[:12]
        branch_name = f"attempt/{task_slug}/{claim.attempt_number}-{attempt_suffix}"
        worktree_path = resolved_worktree_root / task_slug / claim.attempt_id
        artifact_root = resolved_artifact_base / claim.attempt_id
        lock_path = artifact_root / ATTEMPT_LOCK_FILENAME
        pid_path = artifact_root / ATTEMPT_PID_FILENAME
        now = utc_now_iso()
        pid = os.getpid()

        artifact_root.mkdir(parents=True, exist_ok=False)
        snapshotted_inputs: list[str] = []
        if resolved_artifact_base.exists():
            for filename in ATTEMPT_INPUT_FILENAMES:
                source = resolved_artifact_base / filename
                if source.is_file() and not source.is_symlink():
                    shutil.copy2(source, artifact_root / filename)
                    snapshotted_inputs.append(filename)
        lock = AttemptFileLock(lock_path)
        metadata = {
            "attempt_id": claim.attempt_id,
            "lease_id": claim.lease_id,
            "owner_id": claim.owner_id,
            "pid": pid,
            "acquired_at": now,
        }
        if not lock.acquire(blocking=False, metadata=metadata):
            raise AttemptResourceError(f"Attempt lock is already held: {lock_path}")
        atomic_write_json(pid_path, {"kind": "attempt_runtime_pid", **metadata}, sort_keys=True)

        try:
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO attempt_resources(
                        attempt_id, task_id, task_key, attempt_number, owner_id,
                        repo_path, base_branch, base_sha, branch_name,
                        worktree_root, worktree_path, artifact_base_root,
                        artifact_root, lock_path, pid_path, runtime_pid, status,
                        allocated_at, activated_at, released_at, reaped_at,
                        updated_at, release_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?,
                            'allocated', ?, NULL, NULL, NULL, ?, NULL)
                    """,
                    (
                        claim.attempt_id,
                        claim.task_id,
                        claim.task_key,
                        claim.attempt_number,
                        claim.owner_id,
                        str(repo_path),
                        normalized_branch,
                        branch_name,
                        str(resolved_worktree_root),
                        str(worktree_path),
                        str(resolved_artifact_base),
                        str(artifact_root),
                        str(lock_path),
                        str(pid_path),
                        pid,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE attempts
                    SET worktree_path = ?, artifact_root = ?, updated_at = ?
                    WHERE attempt_id = ? AND is_active = 1
                    """,
                    (str(worktree_path), str(artifact_root), now, claim.attempt_id),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET artifact_dir = ?, updated_at = ?, last_synced_at = ?
                    WHERE task_id = ? AND active_attempt_id = ?
                    """,
                    (str(artifact_root), now, now, claim.task_id, claim.attempt_id),
                )
        except BaseException:
            try:
                pid_path.unlink(missing_ok=True)
            finally:
                lock.release()
            raise

        record = self.get(claim.attempt_id)
        assert record is not None
        atomic_write_json(
            artifact_root / ATTEMPT_RESOURCE_MANIFEST,
            {
                "kind": "attempt_resources",
                **record.to_dict(),
                "lease_id": claim.lease_id,
                "input_snapshot": snapshotted_inputs,
            },
            sort_keys=True,
        )
        return AttemptResourceHandle(record=record, lock=lock)

    def provision_workspace(
        self,
        handle: AttemptResourceHandle,
        *,
        store: TaskMirrorStore,
    ) -> WorkspacePreparationResult:
        """Create or idempotently reopen only this Attempt's unique worktree."""
        record = self.get(handle.record.attempt_id) or handle.record
        repo_check = _git(["rev-parse", "--show-toplevel"], record.repo_path)
        if repo_check.returncode != 0:
            return self._blocked(record, f"repo_path is not a git repository: {_git_message(repo_check)}")
        git_root = Path(repo_check.stdout.decode().strip()).resolve()
        if git_root != record.repo_path.resolve():
            return self._blocked(record, "repo_path must be the git repository root")
        base = _git(["rev-parse", record.base_branch], record.repo_path)
        if base.returncode != 0:
            return self._blocked(record, f"base ref could not be resolved: {_git_message(base)}")
        base_sha = base.stdout.decode().strip()

        entries = _worktrees(record.repo_path)
        target = next(
            (
                entry
                for entry in entries
                if entry.get("worktree")
                and Path(entry["worktree"]).resolve() == record.worktree_path.resolve()
            ),
            None,
        )
        if record.worktree_path.exists():
            if target is None or target.get("branch") != f"refs/heads/{record.branch_name}":
                return self._blocked(record, "Attempt worktree path exists with mismatched registration")
            dirty = _git(["status", "--porcelain=v1", "-z"], record.worktree_path)
            if dirty.returncode != 0 or dirty.stdout:
                return self._blocked(record, "Existing same-Attempt worktree is dirty or unreadable")
            return self._activate(record, base_sha, store, WORKSPACE_REUSED)

        branch = _git(["branch", "--list", record.branch_name], record.repo_path)
        if branch.returncode != 0 or branch.stdout.strip():
            return self._blocked(record, f"Attempt branch already exists without its worktree: {record.branch_name}")
        record.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        created = _git(
            [
                "worktree",
                "add",
                str(record.worktree_path),
                "-b",
                record.branch_name,
                record.base_branch,
            ],
            record.repo_path,
        )
        if created.returncode != 0:
            return self._blocked(record, f"git worktree add failed: {_git_message(created)}")
        return self._activate(record, base_sha, store, WORKSPACE_PREPARED)

    def _activate(
        self,
        record: AttemptResourceRecord,
        base_sha: str,
        store: TaskMirrorStore,
        workspace_status: str,
    ) -> WorkspacePreparationResult:
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE attempt_resources
                SET base_sha = ?, status = 'active',
                    activated_at = COALESCE(activated_at, ?), updated_at = ?
                WHERE attempt_id = ?
                """,
                (base_sha, now, now, record.attempt_id),
            )
            conn.execute(
                """
                UPDATE attempts
                SET base_commit = ?, worktree_path = ?, artifact_root = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    base_sha,
                    str(record.worktree_path),
                    str(record.artifact_root),
                    now,
                    record.attempt_id,
                ),
            )
        store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=record.task_key,
                repo_path=record.repo_path,
                worktree_path=record.worktree_path,
                branch=record.branch_name,
                base_branch=record.base_branch,
                base_sha=base_sha,
                status="active",
            )
        )
        return WorkspacePreparationResult(
            task_key=record.task_key,
            repo_path=record.repo_path,
            worktree_path=record.worktree_path,
            branch=record.branch_name,
            base_branch=record.base_branch,
            base_sha=base_sha,
            status=workspace_status,
            summary=(
                f"Prepared Attempt-scoped worktree {record.worktree_path} "
                f"on {record.branch_name} from {record.base_branch}@{base_sha}"
                if workspace_status == WORKSPACE_PREPARED
                else f"Reused the same Attempt's clean worktree: {record.worktree_path}"
            ),
        )

    @staticmethod
    def _blocked(record: AttemptResourceRecord, summary: str) -> WorkspacePreparationResult:
        return WorkspacePreparationResult(
            task_key=record.task_key,
            repo_path=record.repo_path,
            worktree_path=record.worktree_path,
            branch=record.branch_name,
            base_branch=record.base_branch,
            base_sha=record.base_sha,
            status=WORKSPACE_BLOCKED,
            summary=summary,
        )

    def heartbeat(self, attempt_id: str) -> None:
        self.init_db()
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE attempt_resources
                SET runtime_pid = ?, updated_at = ?
                WHERE attempt_id = ? AND status IN ('allocated', 'active')
                """,
                (os.getpid(), now, attempt_id),
            )

    def release(self, handle: AttemptResourceHandle, *, reason: str) -> AttemptResourceRecord:
        """Release process markers but retain branch/worktree/artifact evidence."""
        record = self.get(handle.record.attempt_id) or handle.record
        self._remove_matching_pid(record)
        handle.lock.release()
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE attempt_resources
                SET status = 'released', released_at = COALESCE(released_at, ?),
                    updated_at = ?, release_reason = ?
                WHERE attempt_id = ?
                """,
                (now, now, reason, record.attempt_id),
            )
        updated = self.get(record.attempt_id)
        assert updated is not None
        return updated

    @staticmethod
    def _read_pid(record: AttemptResourceRecord) -> int | None:
        try:
            payload = json.loads(record.pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return record.runtime_pid
        if payload.get("attempt_id") != record.attempt_id:
            return None
        try:
            return int(payload["pid"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _remove_matching_pid(record: AttemptResourceRecord) -> None:
        try:
            payload = json.loads(record.pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if payload.get("attempt_id") == record.attempt_id:
            record.pid_path.unlink(missing_ok=True)

    def reap_stale_resources(self) -> dict[str, list[str]]:
        """Reap stale lock/PID markers without deleting historical resources."""
        self.init_db()
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT attempt_resources.*
                FROM attempt_resources
                JOIN attempts ON attempts.attempt_id = attempt_resources.attempt_id
                WHERE attempt_resources.status IN (
                    'allocated', 'active', 'reap_blocked_live_pid'
                )
                  AND (
                    attempts.is_active = 0
                    OR NOT EXISTS (
                        SELECT 1 FROM runtime_leases
                        WHERE runtime_leases.attempt_id = attempt_resources.attempt_id
                          AND runtime_leases.is_active = 1
                          AND julianday(runtime_leases.expires_at) > julianday(?)
                    )
                  )
                ORDER BY attempt_resources.attempt_number, attempt_resources.attempt_id
                """,
                (now,),
            ).fetchall()
        reaped: list[str] = []
        blocked_live_pid: list[str] = []
        for row in rows:
            record = _row_to_record(row)
            pid = self._read_pid(record)
            if self._pid_alive(pid):
                self._mark_reap_status(record.attempt_id, "reap_blocked_live_pid", now)
                blocked_live_pid.append(record.attempt_id)
                continue
            lock = AttemptFileLock(record.lock_path)
            acquired = lock.acquire(
                blocking=False,
                metadata={
                    "attempt_id": record.attempt_id,
                    "owner_id": "attempt_resource_reaper",
                    "pid": os.getpid(),
                    "reaped_at": now,
                },
            )
            if not acquired:
                self._mark_reap_status(record.attempt_id, "reap_blocked_live_pid", now)
                blocked_live_pid.append(record.attempt_id)
                continue
            try:
                self._remove_matching_pid(record)
            finally:
                lock.release()
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE attempt_resources
                    SET status = 'reaped', reaped_at = ?, updated_at = ?,
                        release_reason = COALESCE(release_reason, 'attempt_resource_reaped')
                    WHERE attempt_id = ?
                    """,
                    (now, now, record.attempt_id),
                )
            reaped.append(record.attempt_id)
        return {"reaped_attempt_ids": reaped, "blocked_live_pid_attempt_ids": blocked_live_pid}

    def _mark_reap_status(self, attempt_id: str, status: str, now: str) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE attempt_resources
                SET status = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (status, now, attempt_id),
            )


__all__ = [
    "ATTEMPT_INPUT_FILENAMES",
    "ATTEMPT_LOCK_FILENAME",
    "ATTEMPT_PID_FILENAME",
    "ATTEMPT_RESOURCE_MANIFEST",
    "AttemptFileLock",
    "AttemptResourceError",
    "AttemptResourceHandle",
    "AttemptResourceManager",
    "AttemptResourceRecord",
]
