from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "summarize_waiting_approval.py"


class SummarizeWaitingApprovalScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.worktree_root = self.root / "worktrees"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _task_key(self) -> str:
        return "AT-GH-901"

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=901,
            title="Summarize waiting approval task",
            body="Task body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/901",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_waiting_task(self, *, status: str = "waiting_approval") -> Path:
        task_key = self._task_key()
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = self.worktree_root / task_key
        worktree_path.mkdir(parents=True, exist_ok=True)

        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Task {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo,
                worktree_path=worktree_path,
                branch=f"task/{task_key}",
                base_branch="main",
                base_sha="deadbeef",
                status="active",
                created_at="2026-05-01T00:00:00Z",
            )
        )

        issue_spec_path = artifact_dir / "issue_spec.md"
        issue_spec_path.write_text(
            render_issue_spec(
                repo="anderson930420/agent-taskflow",
                task_key=task_key,
                issue=self._issue_snapshot(),
                ingested_at="2026-05-03T00:00:00Z",
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(task_key, "issue_spec", issue_spec_path)

        executor_log = artifact_dir / "executor.log"
        executor_log.write_text("executor log\n", encoding="utf-8")
        run_id = self.store.create_executor_run(
            task_key,
            "noop",
            model="gpt-4.1",
            prompt_path=artifact_dir / "prompt.md",
        )
        self.store.finish_executor_run(
            task_key,
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="executor summary",
            log_path=executor_log,
            artifacts={"log": executor_log},
        )
        self.store.record_task_artifact(task_key, "worker_log", executor_log)

        validator_log = artifact_dir / "pytest.log"
        validator_log.write_text("validator log\n", encoding="utf-8")
        self.store.record_validation_result(
            task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="validator summary",
            log_path=validator_log,
            artifacts={"log": validator_log},
        )
        self.store.record_task_artifact(task_key, "review_log", validator_log)

        return artifact_dir

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--db-path", str(self.db_path), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_script_requires_task_key(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)

        missing = self._run("--json")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--task-key", missing.stderr)

    def test_script_prints_valid_json(self) -> None:
        self._seed_waiting_task()

        result = self._run("--task-key", self._task_key(), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], self._task_key())
        self.assertTrue(payload["review_readiness"]["ready_for_human_review"])

    def test_script_supports_pretty(self) -> None:
        self._seed_waiting_task()

        result = self._run("--task-key", self._task_key(), "--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\n  ", result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], self._task_key())

    def test_script_summarizes_waiting_approval_task(self) -> None:
        self._seed_waiting_task()

        result = self._run("--task-key", self._task_key(), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["task"]["status"], "waiting_approval")
        self.assertTrue(payload["review_readiness"]["ready_for_human_review"])

    def test_script_rejects_non_waiting_task_by_default(self) -> None:
        self._seed_waiting_task(status="blocked")

        result = self._run("--task-key", self._task_key(), "--json")

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["review_readiness"]["ready_for_human_review"])

    def test_script_handles_missing_db_without_creating_file(self) -> None:
        missing_db = self.root / "missing.db"
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                self._task_key(),
                "--db-path",
                str(missing_db),
                "--json",
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "not_found")
        self.assertFalse(missing_db.exists())

    def test_script_does_not_write_db(self) -> None:
        self._seed_waiting_task()
        before_events = len(self.store.list_task_events(self._task_key()))
        before_artifacts = len(self.store.list_task_artifacts(self._task_key()))
        before_mtime = self.db_path.stat().st_mtime_ns

        result = self._run("--task-key", self._task_key(), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before_events, len(self.store.list_task_events(self._task_key())))
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts(self._task_key())))
        self.assertEqual(before_mtime, self.db_path.stat().st_mtime_ns)

    def test_script_does_not_write_artifacts(self) -> None:
        artifact_dir = self._seed_waiting_task()
        before = sorted(path.name for path in artifact_dir.iterdir())

        result = self._run("--task-key", self._task_key(), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        after = sorted(path.name for path in artifact_dir.iterdir())
        self.assertEqual(before, after)

    def test_script_source_does_not_include_mutation_helpers(self) -> None:
        script_text = SCRIPT.read_text(encoding="utf-8").lower()
        module_text = (
            REPO_ROOT / "agent_taskflow" / "waiting_approval_summary.py"
        ).read_text(encoding="utf-8").lower()
        combined = script_text + "\n" + module_text

        forbidden = [
            "prepare_task_workspace",
            "dispatch_task",
            "update_task_status",
            "upsert_task",
            "record_task_event",
            "record_task_artifact",
            "create_executor_run",
            "finish_executor_run",
            "record_validation_result",
            "git push",
            "gh pr create",
            "gh pr merge",
            "merge_pull_request",
            "create_pull_request",
            "delete_worktree",
            "delete_branch",
        ]
        for item in forbidden:
            self.assertNotIn(item, combined)


if __name__ == "__main__":
    unittest.main()
