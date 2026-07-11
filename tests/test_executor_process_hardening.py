from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from agent_taskflow.executor_launch import (
    ExecutorLaunchSpec,
    ExecutorProcessStore,
    ProcStat,
    run_managed_process,
)
from agent_taskflow.executor_process_runtime_path import ExecutorProcessRuntimeTaskStore
from agent_taskflow.executors.base import ExecutorContext
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect


class ExecutorProcessHardeningTests(unittest.TestCase):
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
        self.artifact_base = self.root / "artifacts" / "AT-PR7-HARDEN"
        self.artifact_base.mkdir(parents=True)
        (self.artifact_base / "issue_spec.md").write_text(
            "issue\n", encoding="utf-8"
        )
        mirror = TaskMirrorStore(self.db_path)
        mirror.init_db()
        mirror.upsert_task(
            TaskRecord(
                task_key="AT-PR7-HARDEN",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Process hardening",
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
            "AT-PR7-HARDEN",
            source="test-runtime",
            artifact_base_root=self.artifact_base,
            worktree_root=self.repo / ".worktrees",
            base_branch="main",
        )
        workspace = self.store.prepare_attempt_workspace("AT-PR7-HARDEN")
        self.assertTrue(workspace.ok, workspace.summary)
        self.resource = self.store.attempt_resource("AT-PR7-HARDEN")
        self.claim = self.store.runtime_claim("AT-PR7-HARDEN")
        assert self.resource is not None and self.claim is not None
        context = ExecutorContext(
            task_key="AT-PR7-HARDEN",
            project="agent-taskflow",
            worktree_path=self.resource.worktree_path,
            artifact_dir=self.resource.artifact_root,
            repo_root=self.resource.repo_path,
        )
        self.context = self.store.bind_executor_context(context)
        assert self.context.launch_binding is not None
        self.binding = self.context.launch_binding

    def tearDown(self) -> None:
        try:
            if self.store.runtime_claim("AT-PR7-HARDEN") is not None:
                self.store.update_task_status(
                    "AT-PR7-HARDEN",
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

    def test_unknown_linux_process_state_fails_closed_as_live(self) -> None:
        self.assertTrue(
            ProcStat(pid=1, state="K", pgrp=1, session_id=1, start_ticks=1).live
        )
        self.assertFalse(
            ProcStat(pid=1, state="Z", pgrp=1, session_id=1, start_ticks=1).live
        )
        self.assertFalse(
            ProcStat(pid=1, state="X", pgrp=1, session_id=1, start_ticks=1).live
        )

    def test_preflight_failure_still_writes_pid_manifest(self) -> None:
        wrong = self.root / "wrong-cwd"
        wrong.mkdir()
        result = run_managed_process(
            self.binding,
            ExecutorLaunchSpec(
                executor_name="hardening-preflight",
                argv=(sys.executable, "-c", "print('must-not-run')"),
                cwd=wrong,
                artifact_dir=self.resource.artifact_root,
                timeout_seconds=5,
                stdin_mode="devnull",
                combined_output=True,
            ),
            stdout_path=self.resource.artifact_root / "preflight.log",
        )
        self.assertTrue(result.preflight_errors)
        self.assertTrue(result.pid_manifest_path.is_file())
        payload = json.loads(result.pid_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "preflight_failed")
        self.assertIsNone(payload["pid"])
        self.assertEqual(payload["attempt_id"], self.claim.attempt_id)

    def test_exit_unverified_can_be_reconciled_to_verified_exit(self) -> None:
        process_store = ExecutorProcessStore(self.db_path)
        process_id = f"process-{uuid4().hex}"
        launch_path = self.resource.artifact_root / "reconcile-launch.json"
        pid_path = self.resource.artifact_root / "reconcile.pid.json"
        launch_path.write_text("{}\n", encoding="utf-8")
        pid_path.write_text("{}\n", encoding="utf-8")
        process_store.create(
            process_id=process_id,
            binding=self.binding,
            executor_name="reconcile",
            state="allocated",
            launch_spec_path=launch_path,
            pid_manifest_path=pid_path,
            reason_code="executor_launch_allocated",
        )
        process_store.mark_running(
            process_id,
            pid=999999,
            pgid=999999,
            session_id=999999,
            leader_start_ticks=1,
            actor=self.claim.owner_id,
        )
        unverified = process_store.finalize(
            process_id,
            actor=self.claim.owner_id,
            exit_code=None,
            verified_exit=False,
            termination_reason="executor_process_exit_unverified",
        )
        self.assertEqual(unverified.state, "exit_unverified")
        self.assertFalse(unverified.verified_exit)

        verified = process_store.finalize(
            process_id,
            actor=self.claim.owner_id,
            exit_code=0,
            verified_exit=True,
            termination_reason="executor_process_exit_unverified",
            metadata={"reconciled": True},
        )
        self.assertEqual(verified.state, "exited")
        self.assertTrue(verified.verified_exit)
        self.assertEqual(verified.exit_code, 0)
        with connect(self.db_path) as conn:
            transition = conn.execute(
                """
                SELECT from_state, to_state, reason_code
                FROM executor_process_events
                WHERE process_id = ?
                ORDER BY event_id DESC LIMIT 1
                """,
                (process_id,),
            ).fetchone()
        self.assertEqual(transition["from_state"], "exit_unverified")
        self.assertEqual(transition["to_state"], "exited")
        self.assertEqual(
            transition["reason_code"], "executor_process_exit_verified"
        )


if __name__ == "__main__":
    unittest.main()
