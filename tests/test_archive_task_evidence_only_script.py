from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import archive_task_evidence_only as script


class ArchiveTaskEvidenceOnlyScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-ARCHIVE-001"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(self, *, status: str = "waiting_approval") -> None:
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Evidence-only archive candidate",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                exit_code = script.main(argv)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _base_args(self, *, reason_code: str = "smoke_evidence_only") -> list[str]:
        return [
            "--task-key",
            self.task_key,
            "--reason-code",
            reason_code,
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
        ]

    def test_dry_run_does_not_change_task_status(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["task_status_changed"])
        self.assertFalse(payload["db_written"])
        self.assertFalse(payload["artifact_recorded"])
        self.assertFalse(payload["event_recorded"])
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")
        # No artifact file written on dry-run.
        self.assertFalse(
            (self.artifact_root / "task_evidence_archive").exists()
        )

    def test_requires_confirm_flag_for_actual_archive(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--json"])

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-evidence-archive", payload["error"])
        self.assertFalse(payload["db_written"])
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")

    def test_confirmed_archive_changes_status_to_archived(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-evidence-archive", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "task_evidence_archived")
        self.assertTrue(payload["task_status_changed"])
        self.assertTrue(payload["db_written"])
        self.assertEqual(payload["new_task_status"], "archived")
        self.assertEqual(self.store.get_task(self.task_key).status, "archived")

    def test_artifact_is_written_and_recorded(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args(reason_code="salvaged_by_pr")
            + ["--superseded-by-pr", "78", "--confirm-evidence-archive", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["artifact_recorded"])

        artifact_path = Path(payload["evidence"]["artifact_path"])
        self.assertTrue(artifact_path.is_file())

        recorded = [
            artifact
            for artifact in self.store.list_task_artifacts(self.task_key)
            if artifact.artifact_type == "task_evidence_archive"
        ]
        self.assertEqual(len(recorded), 1)

        written = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(written["reason_code"], "salvaged_by_pr")
        self.assertEqual(written["superseded_by_pr"], "78")
        self.assertFalse(written["is_merged_pr_closeout"])

    def test_task_event_is_recorded(self) -> None:
        self._seed_task()
        self._run_main(self._base_args() + ["--confirm-evidence-archive", "--json"])

        events = [
            event
            for event in self.store.list_task_events(self.task_key)
            if event.event_type == "task_evidence_archived"
        ]
        self.assertEqual(len(events), 1)

    def test_reason_code_is_required(self) -> None:
        self._seed_task()
        exit_code, _stdout, stderr = self._run_main(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--json",
            ]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("--reason-code", stderr)

    def test_reason_code_is_validated(self) -> None:
        self._seed_task()
        exit_code, _stdout, stderr = self._run_main(
            self._base_args(reason_code="not_a_real_reason") + ["--json"]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("--reason-code", stderr)

    def test_missing_task_blocks_safely(self) -> None:
        # DB exists but the task does not.
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-evidence-archive", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "not_found")
        self.assertFalse(payload["db_written"])
        self.assertFalse(payload["artifact_recorded"])

    def test_missing_db_does_not_create_state(self) -> None:
        missing_db = self.root / "missing.db"
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                self.task_key,
                "--reason-code",
                "smoke_evidence_only",
                "--db-path",
                str(missing_db),
                "--confirm-evidence-archive",
                "--json",
            ]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "not_found")
        self.assertFalse(missing_db.exists())

    def test_terminal_task_is_blocked(self) -> None:
        self._seed_task(status="completed")
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-evidence-archive", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(self.store.get_task(self.task_key).status, "completed")

    def test_safety_flags_are_false_for_dangerous_actions(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-evidence-archive", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        safety = payload["safety"]
        for flag in (
            "github_mutated",
            "issue_closed",
            "branch_deleted",
            "worktree_deleted",
            "cleanup_performed",
            "executor_started",
            "validator_started",
            "cron_modified",
            "merge_performed",
            "pr_created",
            "automation_added",
            "scheduler_loop_started",
            "background_worker_started",
            "webhook_started",
            "polling_loop_started",
        ):
            self.assertFalse(safety[flag], f"safety.{flag} must be false")
        # db_written is true only on confirmed success.
        self.assertTrue(safety["db_written"])

    def test_db_written_false_on_dry_run_safety_block(self) -> None:
        self._seed_task()
        _exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run", "--json"]
        )

        payload = json.loads(stdout)
        self.assertFalse(payload["safety"]["db_written"])

    def test_blocks_queued_task_only_without_confirm(self) -> None:
        # A queued task is eligible (non-terminal); without confirm it is blocked
        # but the status is unchanged.
        self._seed_task(status="queued")
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--json"])

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(self.store.get_task(self.task_key).status, "queued")


if __name__ == "__main__":
    unittest.main()
