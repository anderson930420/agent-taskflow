from __future__ import annotations

from contextlib import closing
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from agent_taskflow.executor_launch import (
    ExecutorLaunchSpec,
    ExecutorProcessStore,
    check_executor_launch_preflight,
    inspect_process_group,
)
from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.validator_process_runtime_path import ValidatorProcessRuntimeTaskStore
from agent_taskflow.validator_process_schema import (
    VALIDATOR_PROCESS_MIGRATION,
    migrate_validator_process_lifecycle,
)
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult
from agent_taskflow.validators.command import run_command
from agent_taskflow.validators.compile import CompileValidator


class CommandValidator(Validator):
    """Test double: runs an explicit argv through the shared validator command path."""

    name = "command"

    def __init__(self, command: list[str]) -> None:
        self._command = list(command)

    def run(self, context: ValidatorContext) -> ValidatorResult:
        completed, log_path, error_summary, error_status, process_artifacts = run_command(
            validator_name=self.name,
            command=self._command,
            worktree_path=context.worktree_path,
            artifact_dir=context.artifact_dir,
            timeout_seconds=context.timeout_seconds,
            run_env=None,
            launch_binding=context.launch_binding,
        )
        if completed is None:
            return ValidatorResult(
                validator=self.name,
                status=error_status,
                exit_code=None,
                log_path=log_path,
                summary=error_summary,
                artifacts={"log": log_path, **process_artifacts},
            )
        return ValidatorResult(
            validator=self.name,
            status="passed" if completed.returncode == 0 else "failed",
            exit_code=completed.returncode,
            log_path=log_path,
            summary=f"exit code {completed.returncode}",
            artifacts={"log": log_path, **process_artifacts},
        )


class ValidatorProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")

        self.db_path = self.root / "state.db"
        self.artifact_base = self.root / "artifacts" / "AT-PR9-1"
        self.artifact_base.mkdir(parents=True)
        (self.artifact_base / "issue_spec.md").write_text("issue\n", encoding="utf-8")
        base = TaskMirrorStore(self.db_path)
        base.init_db()
        base.upsert_task(
            TaskRecord(
                task_key="AT-PR9-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Validator process lifecycle",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_base,
                executor="noop",
            )
        )
        self.store = ValidatorProcessRuntimeTaskStore(
            self.db_path,
            heartbeat_interval_seconds=60,
        )
        self.store.preclaim_runtime(
            "AT-PR9-1",
            source="test-runtime",
            artifact_base_root=self.artifact_base,
            worktree_root=self.repo / ".worktrees",
            base_branch="main",
        )
        workspace = self.store.prepare_attempt_workspace("AT-PR9-1")
        self.assertTrue(workspace.ok, workspace.summary)
        self.resource = self.store.attempt_resource("AT-PR9-1")
        self.claim = self.store.runtime_claim("AT-PR9-1")
        assert self.resource is not None and self.claim is not None
        self.raw_context = ValidatorContext(
            task_key="AT-PR9-1",
            project="agent-taskflow",
            worktree_path=self.resource.worktree_path,
            artifact_dir=self.resource.artifact_root,
        )
        self.context = self.store.bind_validator_context(self.raw_context)
        assert self.context.launch_binding is not None

    def tearDown(self) -> None:
        try:
            if self.store.runtime_claim("AT-PR9-1") is not None:
                self.store.update_task_status(
                    "AT-PR9-1",
                    "blocked",
                    source="test",
                    blocked_reason="test cleanup",
                )
        finally:
            self.store.shutdown_runtime_supervisors()
        super().tearDown()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _last_process_row(self):
        with closing(connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT * FROM executor_processes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    def test_migration_is_idempotent_and_installs_process_role(self) -> None:
        migrate_validator_process_lifecycle(self.db_path)
        migrate_validator_process_lifecycle(self.db_path)
        with closing(connect(self.db_path)) as conn:
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (VALIDATOR_PROCESS_MIGRATION,),
            ).fetchone()[0]
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(executor_processes)")
            }
        self.assertEqual(migration_count, 1)
        self.assertIn("process_role", columns)

    def test_bound_command_validator_records_verified_validator_process(self) -> None:
        result = CommandValidator(
            [sys.executable, "-c", "print('validator-managed-ok')"]
        ).run(self.context)

        self.assertEqual(result.status, "passed")
        self.assertIn("launch_spec", result.artifacts)
        self.assertIn("pid_manifest", result.artifacts)
        row = self._last_process_row()
        self.assertEqual(row["process_role"], "validator")
        self.assertEqual(row["state"], "exited")
        self.assertEqual(row["verified_exit"], 1)
        self.assertEqual(row["pid"], row["pgid"])
        self.assertEqual(row["pid"], row["session_id"])
        self.assertIn(
            "validator-managed-ok",
            result.log_path.read_text(encoding="utf-8"),
        )

    def test_bound_compile_validator_records_verified_validator_process(self) -> None:
        (self.context.worktree_path / "module.py").write_text("x = 1\n", encoding="utf-8")

        result = CompileValidator().run(self.context)

        self.assertEqual(result.status, "passed", result.summary)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.log_path.name, "compile.log")
        self.assertIn("launch_spec", result.artifacts)
        self.assertIn("pid_manifest", result.artifacts)
        row = self._last_process_row()
        self.assertEqual(row["process_role"], "validator")
        self.assertEqual(row["executor_name"], "compile")
        self.assertEqual(row["state"], "exited")
        self.assertEqual(row["verified_exit"], 1)
        self.assertEqual(row["exit_code"], 0)

    def test_validator_timeout_kills_sigterm_ignoring_descendant(self) -> None:
        script = (
            "import signal,subprocess,sys,time;"
            "signal.signal(15, signal.SIG_IGN);"
            "subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(15, signal.SIG_IGN); time.sleep(60)']);"
            "print('ready', flush=True);time.sleep(60)"
        )
        context = ValidatorContext(
            task_key=self.context.task_key,
            project=self.context.project,
            worktree_path=self.context.worktree_path,
            artifact_dir=self.context.artifact_dir,
            timeout_seconds=1,
            launch_binding=self.context.launch_binding,
        )

        result = CommandValidator([sys.executable, "-c", script]).run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("timed out", result.summary)
        row = self._last_process_row()
        self.assertEqual(row["process_role"], "validator")
        self.assertEqual(row["state"], "exited")
        self.assertEqual(row["termination_reason"], "validator_timeout")
        self.assertIsNotNone(row["term_sent_at"])
        self.assertIsNotNone(row["kill_sent_at"])
        self.assertEqual(row["verified_exit"], 1)
        snapshot = inspect_process_group(row["pgid"], row["session_id"])
        self.assertTrue(snapshot.verified_exited)

    def test_running_kill_switch_terminates_validator_group(self) -> None:
        holder: dict[str, object] = {}

        def run() -> None:
            holder["result"] = CommandValidator(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            ).run(self.context)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        process_store = ExecutorProcessStore(self.db_path)
        deadline = time.monotonic() + 5
        active = None
        while time.monotonic() < deadline:
            active = process_store.active_for_attempt(self.claim.attempt_id)
            if active is not None and active.state == "running":
                break
            time.sleep(0.05)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.process_role, "validator")

        RuntimeControlStore(self.db_path).request_kill(
            scope_kind="attempt",
            scope_id=self.claim.attempt_id,
            actor="test-operator",
        )
        thread.join(timeout=8)

        self.assertFalse(thread.is_alive())
        result = holder["result"]
        self.assertEqual(result.status, "blocked")
        self.assertIn("operator kill request", result.summary)
        record = process_store.get(active.process_id)
        assert record is not None
        self.assertTrue(record.verified_exit)
        self.assertEqual(record.state, "exited")

    def test_shared_registry_rejects_second_active_managed_process(self) -> None:
        holder: dict[str, object] = {}

        def run() -> None:
            holder["result"] = CommandValidator(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            ).run(self.context)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        process_store = ExecutorProcessStore(self.db_path)
        deadline = time.monotonic() + 5
        active = None
        while time.monotonic() < deadline:
            active = process_store.active_for_attempt(self.claim.attempt_id)
            if active is not None and active.state == "running":
                break
            time.sleep(0.05)
        self.assertIsNotNone(active)

        preflight = check_executor_launch_preflight(
            self.context.launch_binding,
            ExecutorLaunchSpec(
                executor_name="second-executor",
                argv=(sys.executable, "-c", "pass"),
                cwd=self.resource.worktree_path,
                artifact_dir=self.resource.artifact_root,
                timeout_seconds=5,
                stdin_mode="devnull",
                combined_output=True,
                process_role="executor",
            ),
        )
        self.assertFalse(preflight.ok)
        self.assertTrue(
            any("active managed process" in item for item in preflight.blocking_errors)
        )

        RuntimeControlStore(self.db_path).request_kill(
            scope_kind="attempt",
            scope_id=self.claim.attempt_id,
            actor="test-cleanup",
        )
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive())

    def test_unbound_local_validator_preserves_legacy_subprocess_path(self) -> None:
        before = len(ExecutorProcessStore(self.db_path).list_active())
        result = CommandValidator([sys.executable, "-c", "print('legacy-local')"]).run(
            self.raw_context
        )
        with closing(connect(self.db_path)) as conn:
            process_count = conn.execute(
                "SELECT COUNT(*) FROM executor_processes"
            ).fetchone()[0]

        self.assertEqual(result.status, "passed")
        self.assertEqual(before, 0)
        self.assertEqual(process_count, 0)
        self.assertNotIn("launch_spec", result.artifacts)


if __name__ == "__main__":
    unittest.main()
