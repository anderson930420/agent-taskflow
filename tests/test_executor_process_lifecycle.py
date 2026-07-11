from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from agent_taskflow.executor_launch import (
    ExecutorLaunchBinding,
    ExecutorLaunchSpec,
    ExecutorProcessStore,
    check_executor_launch_preflight,
    inspect_process_group,
    run_managed_process,
    terminate_registered_process,
)
from agent_taskflow.executor_process_runtime_path import ExecutorProcessRuntimeTaskStore
from agent_taskflow.executor_process_schema import (
    EXECUTOR_PROCESS_MIGRATION,
    migrate_executor_process_lifecycle,
)
from agent_taskflow.executors.base import ExecutorContext
from agent_taskflow.executors.shell import ShellExecutor
from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect


class ExecutorProcessLifecycleTests(unittest.TestCase):
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
        self.artifact_base = self.root / "artifacts" / "AT-PR7-1"
        self.artifact_base.mkdir(parents=True)
        (self.artifact_base / "issue_spec.md").write_text("issue\n", encoding="utf-8")
        base = TaskMirrorStore(self.db_path)
        base.init_db()
        base.upsert_task(
            TaskRecord(
                task_key="AT-PR7-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Process lifecycle",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_base,
                executor="shell",
            )
        )
        self.store = ExecutorProcessRuntimeTaskStore(
            self.db_path,
            heartbeat_interval_seconds=60,
        )
        self.store.preclaim_runtime(
            "AT-PR7-1",
            source="test-runtime",
            artifact_base_root=self.artifact_base,
            worktree_root=self.repo / ".worktrees",
            base_branch="main",
        )
        workspace = self.store.prepare_attempt_workspace("AT-PR7-1")
        self.assertTrue(workspace.ok, workspace.summary)
        self.resource = self.store.attempt_resource("AT-PR7-1")
        self.claim = self.store.runtime_claim("AT-PR7-1")
        assert self.resource is not None and self.claim is not None
        raw_context = ExecutorContext(
            task_key="AT-PR7-1",
            project="agent-taskflow",
            worktree_path=self.resource.worktree_path,
            artifact_dir=self.resource.artifact_root,
            repo_root=self.resource.repo_path,
        )
        self.context = self.store.bind_executor_context(raw_context)
        assert self.context.launch_binding is not None
        self.binding = self.context.launch_binding

    def tearDown(self) -> None:
        try:
            if self.store.runtime_claim("AT-PR7-1") is not None:
                self.store.update_task_status(
                    "AT-PR7-1",
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

    def _spec(
        self,
        argv: tuple[str, ...],
        *,
        timeout: int | None = None,
        term_grace: float = 0.15,
        kill_wait: float = 1.0,
    ) -> ExecutorLaunchSpec:
        return ExecutorLaunchSpec(
            executor_name="test-executor",
            argv=argv,
            cwd=self.resource.worktree_path,
            artifact_dir=self.resource.artifact_root,
            timeout_seconds=timeout,
            stdin_mode="devnull",
            combined_output=True,
            terminate_grace_seconds=term_grace,
            kill_wait_seconds=kill_wait,
        )

    def test_migration_is_idempotent_and_installs_process_guards(self) -> None:
        migrate_executor_process_lifecycle(self.db_path)
        migrate_executor_process_lifecycle(self.db_path)
        with closing(connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (EXECUTOR_PROCESS_MIGRATION,),
            ).fetchone()[0]
            triggers = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'trigger' AND name LIKE 'executor_process%'
                    """
                )
            }
        self.assertEqual(count, 1)
        self.assertIn("executor_process_state_guard", triggers)
        self.assertIn("executor_process_events_no_update", triggers)
        self.assertIn("executor_process_events_no_delete", triggers)

    def test_canonical_shell_launch_records_isolated_verified_process(self) -> None:
        result = ShellExecutor(
            [sys.executable, "-c", "print('managed-ok')"],
            name="managed-shell",
        ).run(self.context)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        records = ExecutorProcessStore(self.db_path).list_active()
        self.assertEqual(records, [])
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM executor_processes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["state"], "exited")
        self.assertEqual(row["verified_exit"], 1)
        self.assertEqual(row["pid"], row["pgid"])
        self.assertEqual(row["pid"], row["session_id"])
        self.assertTrue(Path(row["launch_spec_path"]).is_file())
        self.assertTrue(Path(row["pid_manifest_path"]).is_file())
        self.assertIn("managed-ok", result.log_path.read_text(encoding="utf-8"))

    def test_preflight_rejects_non_attempt_cwd_without_starting_process(self) -> None:
        wrong = self.root / "wrong"
        wrong.mkdir()
        spec = replace(self._spec((sys.executable, "-c", "print('no')")), cwd=wrong)
        preflight = check_executor_launch_preflight(self.binding, spec)
        self.assertFalse(preflight.ok)
        self.assertIn(
            "launch cwd does not match the active Attempt worktree",
            preflight.blocking_errors,
        )
        result = run_managed_process(
            self.binding,
            spec,
            stdout_path=self.resource.artifact_root / "wrong.log",
        )
        self.assertTrue(result.preflight_errors)
        record = ExecutorProcessStore(self.db_path).get(result.process_id)
        assert record is not None
        self.assertEqual(record.state, "preflight_failed")
        self.assertIsNone(record.pid)

    def test_timeout_kills_sigterm_ignoring_descendant_and_verifies_exit(self) -> None:
        script = (
            "import signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']);"
            "print('ready', flush=True);time.sleep(60)"
        )
        result = run_managed_process(
            self.binding,
            self._spec((sys.executable, "-c", script), timeout=1),
            stdout_path=self.resource.artifact_root / "timeout.log",
        )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.term_sent)
        self.assertTrue(result.kill_sent)
        self.assertTrue(result.verified_exit)
        record = ExecutorProcessStore(self.db_path).get(result.process_id)
        assert record is not None
        self.assertEqual(record.state, "exited")
        self.assertEqual(record.termination_reason, "executor_timeout")
        snapshot = inspect_process_group(record.pgid, record.session_id)
        self.assertTrue(snapshot.verified_exited)

    def test_running_kill_switch_terminates_process_group(self) -> None:
        holder: dict[str, object] = {}

        def run() -> None:
            holder["result"] = run_managed_process(
                self.binding,
                self._spec((sys.executable, "-c", "import time; time.sleep(60)")),
                stdout_path=self.resource.artifact_root / "kill-switch.log",
            )

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
        RuntimeControlStore(self.db_path).request_kill(
            scope_kind="attempt",
            scope_id=self.claim.attempt_id,
            actor="test-operator",
        )
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        result = holder["result"]
        self.assertTrue(result.kill_requested)
        self.assertTrue(result.verified_exit)

    def test_external_hard_termination_escalates_and_verifies_exit(self) -> None:
        holder: dict[str, object] = {}
        script = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('ready', flush=True);time.sleep(60)"
        )

        def run() -> None:
            holder["result"] = run_managed_process(
                self.binding,
                self._spec((sys.executable, "-c", script)),
                stdout_path=self.resource.artifact_root / "external-kill.log",
            )

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
        assert active is not None
        RuntimeControlStore(self.db_path).request_kill(
            scope_kind="attempt",
            scope_id=self.claim.attempt_id,
            actor="external-operator",
        )
        terminated = terminate_registered_process(
            process_store,
            active,
            actor="external-operator",
            termination_reason="operator_kill_requested",
            terminate_grace_seconds=0.15,
            kill_wait_seconds=1.0,
        )
        self.assertTrue(terminated.verified_exit)
        self.assertIsNotNone(terminated.term_sent_at)
        self.assertIsNotNone(terminated.kill_sent_at)
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())

    def test_process_events_are_append_only(self) -> None:
        result = run_managed_process(
            self.binding,
            self._spec((sys.executable, "-c", "pass")),
            stdout_path=self.resource.artifact_root / "immutable.log",
        )
        with closing(connect(self.db_path)) as conn:
            event_id = conn.execute(
                """
                SELECT event_id FROM executor_process_events
                WHERE process_id = ? ORDER BY event_id LIMIT 1
                """,
                (result.process_id,),
            ).fetchone()[0]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "executor process events are append-only",
            ):
                with conn:
                    conn.execute(
                        "UPDATE executor_process_events SET actor = 'rewritten' WHERE event_id = ?",
                        (event_id,),
                    )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "executor process events are append-only",
            ):
                with conn:
                    conn.execute(
                        "DELETE FROM executor_process_events WHERE event_id = ?",
                        (event_id,),
                    )


if __name__ == "__main__":
    unittest.main()
