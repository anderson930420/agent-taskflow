"""Shared fixtures for the V1 Step 5 acceptance tests.

Not a test module (no ``test_`` prefix). Every database, repository and
artifact directory lives in a TemporaryDirectory.

RULINGS 67: a Ticket runs only under its project's execution policy, so the
fixture registers the ``step5`` project with a policy whose executor is the fake
claude executable (``tests/fake_claude_executable.py``), and points the resolver
at that registry until ``cleanup()``. ``RecordingExecutor`` records the context
and, when it is to complete, runs that real (fake) executable through the
managed launch, because only a verified invocation that changes the worktree
may reach ``ready_for_integration``.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
from unittest import mock

import yaml

import agent_taskflow  # noqa: F401  installs the layered runtime path
from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.execution_policy import resolve_execution_policy
from agent_taskflow.executors.base import ExecutorContext, ExecutorResult
from agent_taskflow.executors.claude_code import ClaudeCodeExecutor
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_progress_schema import migrate_runtime_progress
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_creation import TicketCreationRequest, create_ticket
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields
from agent_taskflow.ticket_lifecycle import is_ticket
from agent_taskflow.ticket_repositories import TicketRepository
from agent_taskflow.ticket_store import TicketStore
from agent_taskflow.ticket_worktree_schema import migrate_ticket_worktree_resources
from agent_taskflow.validators.base import ValidatorContext, ValidatorResult
from agent_taskflow.validators.registry import list_validator_names

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_policy_support import (  # noqa: E402
    policy_block,
    project_entry,
    use_registry,
    write_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_SCRIPT = Path(__file__).resolve().parent / "step5_scheduler_worker.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def git_worktrees(repo: Path) -> list[tuple[Path, str | None]]:
    """Return (path, branch) for every worktree except the main checkout."""
    out = git(repo, "worktree", "list", "--porcelain")
    entries: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1]
        elif not line and path is not None:
            entries.append((path, branch))
            path, branch = None, None
    return [entry for entry in entries if entry[0].resolve() != repo.resolve()]


POLICY_EXECUTOR = "claude-code"
POLICY_VALIDATOR = "pytest"


class RecordingExecutor:
    """In-process executor that records the context and returns a fixed status.

    For ``completed`` it runs the policy's fake claude executable for real, so
    the dispatcher sees a managed launch, exit 0 and a worktree change.
    """

    def __init__(
        self,
        status: str = "completed",
        *,
        summary: str | None = None,
        raise_exc: BaseException | None = None,
        write_file: str | None = None,
    ) -> None:
        self.name = POLICY_EXECUTOR
        self.status = status
        self.summary = summary
        self.raise_exc = raise_exc
        self.write_file = write_file
        self.contexts: list[ExecutorContext] = []
        self.seen_files: list[list[str]] = []

    def run(self, context: ExecutorContext) -> ExecutorResult:
        self.contexts.append(context)
        worktree = Path(context.worktree_path)
        self.seen_files.append(sorted(p.name for p in worktree.iterdir()))
        if self.write_file is not None:
            (worktree / self.write_file).write_text(
                f"written by attempt {context.attempt_id}\n", encoding="utf-8"
            )
        if self.raise_exc is not None:
            raise self.raise_exc
        binding = context.launch_binding
        if self.status == "completed" and binding is not None and is_ticket(binding.db_path, context.task_key):
            policy = resolve_execution_policy(context.project)
            return ClaudeCodeExecutor(
                command=policy.resolved_argv(), enable_invocation=True, model=policy.model,
            ).run(context)
        return ExecutorResult(
            executor=self.name,
            status=self.status,
            summary=self.summary or f"fake executor {self.status}",
        )


class RecordingValidator:
    def __init__(
        self,
        name: str = POLICY_VALIDATOR,
        status: str = "passed",
        *,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.name = name
        self.status = status
        self.raise_exc = raise_exc
        self.contexts: list[ValidatorContext] = []

    def run(self, context: ValidatorContext) -> ValidatorResult:
        self.contexts.append(context)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ValidatorResult(
            validator=self.name,
            status=self.status,
            summary=f"fake validator {self.status}",
        )


@dataclass
class Step5Fixture:
    root: Path
    repo: Path
    db_path: Path
    artifacts: Path
    repository: TicketRepository
    tmp: Any = field(repr=False, default=None)
    registry_path: Path | None = None
    registry_patcher: Any = field(repr=False, default=None)
    validator_names_patcher: Any = field(repr=False, default=None)
    test_validator_names: set[str] = field(default_factory=set)

    def cleanup(self) -> None:
        for name in ("registry_patcher", "validator_names_patcher"):
            patcher = getattr(self, name)
            if patcher is not None:
                patcher.stop()
                setattr(self, name, None)
        if self.tmp is not None:
            self.tmp.cleanup()

    def set_policy_validators(self, names: tuple[str, ...]) -> None:
        """Make ``names`` the step5 policy's implementation validators.

        Test-only validator names are registered as known for this fixture's
        lifetime, so the resolver's strict name check still applies to them.
        """
        self.test_validator_names.update(names)
        self.register_project("step5", self.repo, policy_block(
            implementation_validators=[{"name": name, "timeout_seconds": 60} for name in names],
        ))

    def register_project(self, name: str, repo: Path, execution: dict[str, Any] | None = None) -> None:
        """Add or replace one registry entry, keeping the others."""
        projects = yaml.safe_load(self.registry_path.read_text(encoding="utf-8"))["projects"]
        projects[name] = project_entry(repo, execution=execution or policy_block())
        write_registry(self.registry_path, projects)

    # ------------------------------------------------------------------
    def create_ticket(
        self,
        prompt: str = "Add a feature",
        *,
        priority: str = "normal",
        blocked_by: str | None = None,
    ):
        return create_ticket(
            TicketCreationRequest(
                repository=self.repository.repository,
                prompt=prompt,
                priority=priority,
                blocked_by=blocked_by,
            ),
            store=TicketStore(self.db_path),
            repository=self.repository,
        ).ticket

    def add_legacy_task(self, task_key: str, *, status: str = "queued") -> None:
        artifacts = self.artifacts / task_key
        artifacts.mkdir(parents=True, exist_ok=True)
        TaskMirrorStore(self.db_path).upsert_task(
            TaskRecord(
                task_key=task_key,
                project="step5",
                board="step5",
                title=f"Legacy {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifacts,
                executor="fake",
            )
        )

    def dispatcher(
        self,
        executor: RecordingExecutor | None = None,
        validators: tuple[RecordingValidator, ...] | None = None,
    ) -> Dispatcher:
        executor = executor or RecordingExecutor()
        validators = validators if validators is not None else (RecordingValidator(),)
        # A Ticket runs the policy's validators, so the policy lists the ones
        # this test supplies (an empty tuple keeps the default stand-in).
        self.set_policy_validators(tuple(v.name for v in validators) or (POLICY_VALIDATOR,))
        # A Ticket takes its executor and validator names from the step5
        # policy and ignores `validators`/`default_executor`; a legacy task
        # still uses them. The registries only supply the in-process objects.
        return Dispatcher(
            db_path=self.db_path,
            executor_registry={POLICY_EXECUTOR: executor, "fake": executor},
            # A Ticket always runs the policy's validator; a passing stand-in
            # answers for it unless the test supplies its own.
            validator_registry={POLICY_VALIDATOR: RecordingValidator(), **{v.name: v for v in validators}},
            validators=tuple(v.name for v in validators),
            default_executor="fake",
        )

    def dispatch(self, task_key: str, executor=None, validators=None):
        dispatcher = self.dispatcher(executor, validators)
        try:
            return dispatcher.dispatch_task(task_key)
        finally:
            shutdown = getattr(dispatcher.store, "shutdown_runtime_supervisors", None)
            if shutdown is not None:
                shutdown()

    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def task_row(self, task_key: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
        return dict(row) if row is not None else {}

    def status(self, task_key: str) -> str:
        return self.task_row(task_key)["status"]

    def events(self, task_key: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT event_type, source, message, payload_json FROM task_events"
                " WHERE task_key = ? ORDER BY id",
                (task_key,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(row["payload_json"]) if row["payload_json"] else {}
            result.append(item)
        return result

    def event_kinds(self, task_key: str) -> list[str]:
        return [event["payload"].get("kind", "") for event in self.events(task_key)]

    def status_events(self, task_key: str) -> list[dict[str, Any]]:
        return [e for e in self.events(task_key) if e["event_type"] == "status_changed"]

    def attempts(self, task_key: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "attempts" not in tables:
                return []
            rows = conn.execute(
                "SELECT attempts.* FROM attempts JOIN tasks ON tasks.task_id = attempts.task_id"
                " WHERE tasks.task_key = ? ORDER BY attempt_number",
                (task_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def attempt_resources(self, task_key: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "attempt_resources" not in tables:
                return []
            rows = conn.execute(
                "SELECT * FROM attempt_resources WHERE task_key = ? ORDER BY attempt_number",
                (task_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def leases(self, task_key: str | None = None, *, active_only: bool = False) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "runtime_leases" not in tables:
                return []
            sql = (
                "SELECT runtime_leases.*, tasks.task_key FROM runtime_leases"
                " JOIN tasks ON tasks.task_id = runtime_leases.task_id WHERE 1 = 1"
            )
            params: list[Any] = []
            if task_key is not None:
                sql += " AND tasks.task_key = ?"
                params.append(task_key)
            if active_only:
                sql += " AND runtime_leases.is_active = 1"
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def task_worktree_rows(self, task_key: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM task_worktrees WHERE task_key = ?", (task_key,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, task_key: str, status: str, *, blocked_reason: str | None = None) -> None:
        """Test-only direct write, for seeding a blocker's state."""
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE tasks SET status = ?, blocked_reason = ? WHERE task_key = ?",
                (status, blocked_reason, task_key),
            )


def make_fixture(*, migrate_step5: bool = True, worktrees_dir: Path | None = None) -> Step5Fixture:
    tmp = tempfile.TemporaryDirectory(prefix="v1-step5-")
    root = Path(tmp.name)
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "step5@example.invalid")
    git(repo, "config", "user.name", "Step 5")
    (repo / "README.md").write_text("step5\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    db_path = root / "state.db"
    artifacts = root / "artifacts"
    artifacts.mkdir()
    TaskMirrorStore(db_path).init_db()
    migrate_ticket_fields(db_path)
    migrate_task_attempt_lifecycle(db_path)
    migrate_runtime_progress(db_path)
    if migrate_step5:
        migrate_ticket_worktree_resources(db_path)
    repository = TicketRepository(
        repository="step5",
        repo_path=repo,
        worktrees_dir=worktrees_dir or (repo / ".worktrees"),
        artifacts_root=artifacts,
        base_branch="main",
        branch_prefix="task/",
    )
    registry_path = write_registry(
        root / "projects.yaml",
        {"step5": project_entry(repo, execution=policy_block(
            implementation_validators=[{"name": POLICY_VALIDATOR, "timeout_seconds": 60}],
        ))},
    )
    fixture = Step5Fixture(
        root=root,
        repo=repo,
        db_path=db_path,
        artifacts=artifacts,
        repository=repository,
        tmp=tmp,
        registry_path=registry_path,
        registry_patcher=use_registry(registry_path),
    )
    builtin = list_validator_names()
    fixture.validator_names_patcher = mock.patch(
        "agent_taskflow.validators.registry.list_validator_names",
        side_effect=lambda: [*builtin, *sorted(fixture.test_validator_names - set(builtin))],
    )
    fixture.validator_names_patcher.start()
    return fixture


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) if not existing else f"{REPO_ROOT}{os.pathsep}{existing}"
    return env


def worker_launcher(
    sync_dir: Path,
    *,
    mode: str = "pass",
    expect: int = 1,
    delay: float = 0.0,
    registry_path: Path | None = None,
):
    """Return a scheduler launcher that runs tests/step5_scheduler_worker.py.

    ``registry_path`` is the fixture's policy registry, which the worker points
    its resolver at (defaults to ``sync_dir/../projects.yaml``).
    """
    registry = registry_path or sync_dir.parent / "projects.yaml"

    def launch(db_path: Path, task_key: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                str(WORKER_SCRIPT),
                "--db-path",
                str(db_path),
                "--task-key",
                task_key,
                "--sync-dir",
                str(sync_dir),
                "--mode",
                mode,
                "--expect",
                str(expect),
                "--delay",
                str(delay),
                "--projects-registry",
                str(registry),
            ],
            cwd=REPO_ROOT,
            env=worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    return launch


__all__ = [
    "RecordingExecutor",
    "RecordingValidator",
    "Step5Fixture",
    "git",
    "git_worktrees",
    "make_fixture",
    "worker_env",
    "worker_launcher",
]
