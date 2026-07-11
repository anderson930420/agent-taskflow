from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_status_reset import (
    TaskStatusResetError,
    TaskStatusResetRequest,
    reset_task_status,
)
from scripts import reset_task_status as script


class TaskStatusResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.artifact_dir = self.root / "artifacts" / "AT-RESET-001"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-RESET-001"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        status: str = "blocked",
        with_artifact_dir: bool = True,
    ) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Reset test task",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir if with_artifact_dir else None,
                blocked_reason="operator recovery required",
            )
        )

    def _base_args(self) -> list[str]:
        return [
            "--task-key",
            self.task_key,
            "--db-path",
            str(self.db_path),
            "--from-status",
            "blocked",
            "--reason",
            "retry after operator inspection",
        ]

    def _run_main(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                exit_code = script.main(args)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_confirmed_blocked_to_queued_reset_succeeds(self) -> None:
        self._seed_task()

        exit_code, stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-reset"]
        )

        self.assertEqual(exit_code, 0, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["mutated"])
        self.assertFalse(result["dry_run"])
        task = self.store.get_task(self.task_key)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "queued")
        self.assertIsNone(task.blocked_reason)

    def test_dry_run_does_not_mutate_or_write_audit(self) -> None:
        self._seed_task()

        exit_code, stdout, stderr = self._run_main(
            self._base_args() + ["--dry-run"]
        )

        self.assertEqual(exit_code, 0, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["mutated"])
        self.assertEqual(self.store.get_task(self.task_key).status, "blocked")
        self.assertFalse((self.artifact_dir / "task-status-reset.json").exists())
        reset_notes = [
            event
            for event in self.store.list_task_events(self.task_key)
            if event.source == "reset_task_status_cli" and event.event_type == "note"
        ]
        self.assertEqual(reset_notes, [])

    def test_missing_confirmation_blocks_mutation(self) -> None:
        self._seed_task()

        exit_code, _stdout, stderr = self._run_main(self._base_args())

        self.assertEqual(exit_code, 1)
        self.assertIn("--confirm-reset", stderr)
        self.assertEqual(self.store.get_task(self.task_key).status, "blocked")

    def test_wrong_expected_current_status_blocks_mutation(self) -> None:
        self._seed_task(status="queued")

        exit_code, _stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-reset"]
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("expected 'blocked'", stderr)
        self.assertEqual(self.store.get_task(self.task_key).status, "queued")

    def test_missing_task_exits_nonzero(self) -> None:
        exit_code, _stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-reset"]
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("Task not found", stderr)

    def test_non_blocked_source_status_is_rejected(self) -> None:
        self._seed_task()

        args = self._base_args()
        args[args.index("blocked")] = "queued"
        exit_code, _stdout, stderr = self._run_main(args + ["--confirm-reset"])

        self.assertNotEqual(exit_code, 0)
        self.assertIn("invalid choice", stderr)
        self.assertEqual(self.store.get_task(self.task_key).status, "blocked")

    def test_non_queued_target_status_is_rejected(self) -> None:
        self._seed_task()

        exit_code, _stdout, stderr = self._run_main(
            self._base_args()
            + ["--to-status", "blocked", "--confirm-reset"]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("invalid choice", stderr)
        self.assertEqual(self.store.get_task(self.task_key).status, "blocked")

    def test_reset_records_required_note_event(self) -> None:
        self._seed_task()
        self._run_main(self._base_args() + ["--confirm-reset"])

        reset_notes = [
            event
            for event in self.store.list_task_events(self.task_key)
            if event.source == "reset_task_status_cli" and event.event_type == "note"
        ]
        self.assertEqual(len(reset_notes), 1)
        payload = json.loads(reset_notes[0].payload_json)
        self.assertEqual(payload["kind"], "task_status_reset")
        self.assertEqual(payload["task_key"], self.task_key)
        self.assertEqual(payload["from_status"], "blocked")
        self.assertEqual(payload["to_status"], "queued")
        self.assertEqual(payload["reason"], "retry after operator inspection")
        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["operator_confirmed"])
        self.assertTrue(payload["not_approval"])
        self.assertTrue(payload["not_merge"])
        self.assertTrue(payload["not_cleanup"])
        self.assertTrue(payload["not_validation_authority"])

    def test_reset_writes_and_records_audit_json(self) -> None:
        self._seed_task()
        exit_code, stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-reset"]
        )

        self.assertEqual(exit_code, 0, stderr)
        result = json.loads(stdout)
        artifact_path = Path(result["audit_artifact_path"])
        self.assertEqual(artifact_path.parent.name, "reset-audit")
        self.assertTrue(artifact_path.name.startswith("reset-"))
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["reset_id"], result["reset_id"])
        self.assertEqual(payload["new_attempt_id"], result["new_attempt_id"])
        self.assertEqual(payload["kind"], "task_status_reset")
        recorded = [
            artifact
            for artifact in self.store.list_task_artifacts(self.task_key)
            if artifact.artifact_type == "other" and artifact.path == artifact_path
        ]
        self.assertEqual(len(recorded), 1)

    def test_missing_artifact_dir_does_not_block_reset(self) -> None:
        self._seed_task(with_artifact_dir=False)

        exit_code, stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-reset"]
        )

        self.assertEqual(exit_code, 0, stderr)
        self.assertIsNone(json.loads(stdout)["audit_artifact_path"])
        self.assertEqual(self.store.get_task(self.task_key).status, "queued")

    def test_reset_preserves_worktree_record_and_path(self) -> None:
        self._seed_task()
        worktree_path = self.root / "worktrees" / self.task_key
        worktree_path.mkdir(parents=True)
        sentinel = worktree_path / "keep.txt"
        sentinel.write_text("preserve me\n", encoding="utf-8")
        worktree = TaskWorktreeRecord(
            task_key=self.task_key,
            repo_path=self.repo_path,
            worktree_path=worktree_path,
            branch="task/reset-test",
            base_branch="main",
            base_sha="a" * 40,
            status="active",
        )
        self.store.upsert_task_worktree(worktree)
        recorded_worktree = self.store.get_task_worktree(self.task_key)

        exit_code, _stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-reset"]
        )

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(self.store.get_task_worktree(self.task_key), recorded_worktree)
        self.assertTrue(worktree_path.is_dir())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

    def test_store_expected_status_check_is_compare_and_set_protected(self) -> None:
        self._seed_task()
        self.store.update_task_status(self.task_key, "queued")

        with self.assertRaisesRegex(ValueError, "expected 'blocked'"):
            self.store.update_task_status(
                self.task_key,
                "queued",
                expected_current_status="blocked",
            )

        self.assertEqual(self.store.get_task(self.task_key).status, "queued")

    def test_request_rejects_invalid_transitions_and_empty_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "from_status"):
            TaskStatusResetRequest(
                task_key=self.task_key,
                from_status="waiting_approval",
                to_status="queued",
                reason="reason",
            )
        with self.assertRaisesRegex(ValueError, "to_status"):
            TaskStatusResetRequest(
                task_key=self.task_key,
                from_status="blocked",
                to_status="waiting_approval",
                reason="reason",
            )
        with self.assertRaisesRegex(ValueError, "reason"):
            TaskStatusResetRequest(
                task_key=self.task_key,
                from_status="blocked",
                to_status="queued",
                reason="   ",
            )

    def test_service_reports_status_mismatch(self) -> None:
        self._seed_task(status="queued")
        request = TaskStatusResetRequest(
            task_key=self.task_key,
            db_path=self.db_path,
            from_status="blocked",
            reason="reason",
            confirm_reset=True,
        )

        with self.assertRaisesRegex(TaskStatusResetError, "expected 'blocked'"):
            reset_task_status(request, store=self.store)

    def test_script_help_runs_when_invoked_by_path(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path("scripts") / "reset_task_status.py"),
                "--help",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--confirm-reset", completed.stdout)



if __name__ == "__main__":
    unittest.main()
