"""Tests for agent_taskflow.pr_handoff."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.pr_handoff import (
    PrHandoffError,
    PrHandoffRequest,
    create_pr_handoff,
)
from agent_taskflow.store import TaskMirrorStore


class PrHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_dir = self.root / "artifacts" / "AT-HANDOFF-001"
        self.output_dir = self.root / "handoffs"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha = self._init_git_repo()
        self.worktree = self.root / "worktree"
        self._git(["worktree", "add", "-b", "task/AT-HANDOFF-001", str(self.worktree), "main"])
        (self.worktree / "feature.txt").write_text("handoff change\n", encoding="utf-8")
        self._add_waiting_task()
        self._add_worktree_record()
        self._add_review_evidence()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed

    def _init_git_repo(self) -> str:
        self.repo.mkdir()
        self._git(["init"])
        self._git(["config", "user.email", "agent-taskflow@example.invalid"])
        self._git(["config", "user.name", "Agent Taskflow"])
        (self.repo / "README.md").write_text("# handoff test\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "initial"])
        self._git(["branch", "-M", "main"])
        return self._git(["rev-parse", "main"]).stdout.strip()

    def _add_waiting_task(
        self,
        *,
        task_key: str = "AT-HANDOFF-001",
        status: str = "waiting_approval",
        artifact_dir: Path | None = None,
    ) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="PR handoff test task",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir or self.artifact_dir,
            )
        )

    def _add_worktree_record(
        self,
        *,
        task_key: str = "AT-HANDOFF-001",
        worktree_path: Path | None = None,
        base_sha: str | None = None,
    ) -> None:
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo,
                worktree_path=worktree_path or self.worktree,
                branch=f"task/{task_key}",
                base_branch="main",
                base_sha=base_sha or self.base_sha,
                status="active",
            )
        )

    def _add_review_evidence(self, *, task_key: str = "AT-HANDOFF-001") -> None:
        artifact_dir = self.artifact_dir if task_key == "AT-HANDOFF-001" else self.root / "artifacts" / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result_path = artifact_dir / "result.txt"
        log_path = artifact_dir / "validator.log"
        result_path.write_text("executor result\n", encoding="utf-8")
        log_path.write_text("validator passed\n", encoding="utf-8")
        contract = build_mission_contract(
            task_key=task_key,
            goal="Test PR handoff",
            repo_path=self.repo,
            worktree_path=self.worktree,
            artifact_dir=artifact_dir,
            executor="test-executor",
            required_validators=("test-validator",),
        )
        write_mission_contract(contract, artifact_dir=artifact_dir)
        self.store.record_task_artifact(task_key, "other", result_path)
        run_id = self.store.create_executor_run(task_key, "test-executor")
        self.store.finish_executor_run(
            task_key,
            run_id,
            executor="test-executor",
            status="completed",
            exit_code=0,
            summary="executor completed",
            log_path=result_path,
            artifacts={"result": result_path},
        )
        self.store.record_validation_result(
            task_key,
            "test-validator",
            status="passed",
            exit_code=0,
            summary="validator passed",
            log_path=log_path,
            artifacts={"log": log_path},
        )

    def _request(self, *, dry_run: bool = False) -> PrHandoffRequest:
        return PrHandoffRequest(
            task_key="AT-HANDOFF-001",
            db_path=self.db_path,
            output_dir=self.output_dir,
            repo="anderson930420/agent-taskflow",
            dry_run=dry_run,
        )

    def test_handoff_succeeds_for_waiting_approval_task_with_active_worktree(self) -> None:
        result = create_pr_handoff(self._request())

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "created")
        assert result.package is not None
        self.assertEqual(result.package.data["task_status"], "waiting_approval")
        self.assertEqual(result.package.data["worktree_path"], str(self.worktree))

    def test_handoff_rejects_missing_task(self) -> None:
        with self.assertRaisesRegex(PrHandoffError, "Task not found"):
            create_pr_handoff(
                PrHandoffRequest(
                    task_key="AT-MISSING",
                    db_path=self.db_path,
                    output_dir=self.output_dir,
                )
            )

    def test_handoff_rejects_task_not_waiting_approval(self) -> None:
        self._add_waiting_task(task_key="AT-NOT-READY", status="queued", artifact_dir=self.root / "artifacts" / "AT-NOT-READY")

        with self.assertRaisesRegex(PrHandoffError, "waiting_approval"):
            create_pr_handoff(
                PrHandoffRequest(
                    task_key="AT-NOT-READY",
                    db_path=self.db_path,
                    output_dir=self.output_dir,
                )
            )

    def test_handoff_rejects_missing_worktree_record(self) -> None:
        self._add_waiting_task(task_key="AT-NO-WORKTREE", artifact_dir=self.root / "artifacts" / "AT-NO-WORKTREE")

        with self.assertRaisesRegex(PrHandoffError, "TaskWorktreeRecord missing"):
            create_pr_handoff(
                PrHandoffRequest(
                    task_key="AT-NO-WORKTREE",
                    db_path=self.db_path,
                    output_dir=self.output_dir,
                )
            )

    def test_handoff_rejects_missing_worktree_path(self) -> None:
        self._add_waiting_task(task_key="AT-MISSING-WORKTREE", artifact_dir=self.root / "artifacts" / "AT-MISSING-WORKTREE")
        self._add_worktree_record(
            task_key="AT-MISSING-WORKTREE",
            worktree_path=self.root / "does-not-exist",
        )

        with self.assertRaisesRegex(PrHandoffError, "Worktree path is missing"):
            create_pr_handoff(
                PrHandoffRequest(
                    task_key="AT-MISSING-WORKTREE",
                    db_path=self.db_path,
                    output_dir=self.output_dir,
                )
            )

    def test_handoff_includes_branch_base_and_head(self) -> None:
        result = create_pr_handoff(self._request())

        assert result.package is not None
        package = result.package.data
        self.assertEqual(package["branch"], "task/AT-HANDOFF-001")
        self.assertEqual(package["base_branch"], "main")
        self.assertEqual(package["base_sha"], self.base_sha)
        self.assertEqual(
            package["head_sha"],
            self._git(["rev-parse", "HEAD"], cwd=self.worktree).stdout.strip(),
        )

    def test_handoff_includes_changed_files(self) -> None:
        result = create_pr_handoff(self._request())

        assert result.package is not None
        self.assertIn("feature.txt", result.package.data["changed_files"])

    def test_handoff_includes_validator_executor_and_artifact_summaries(self) -> None:
        result = create_pr_handoff(self._request())

        assert result.package is not None
        package = result.package.data
        self.assertEqual(package["validation_summary"]["count"], 1)
        self.assertTrue(package["validation_summary"]["all_passed"])
        self.assertEqual(package["executor_summary"]["count"], 1)
        self.assertGreaterEqual(package["artifact_summary"]["db_artifact_count"], 1)
        self.assertTrue(package["review_evidence_summary"]["available"])

    def test_handoff_writes_json_and_markdown(self) -> None:
        result = create_pr_handoff(self._request())

        self.assertTrue(result.json_path.is_file())
        self.assertTrue(result.markdown_path.is_file())
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifact_type"], "pr_handoff")
        self.assertIn("This package did not create a PR.", result.markdown_path.read_text(encoding="utf-8"))

    def test_handoff_records_artifact_and_event(self) -> None:
        result = create_pr_handoff(self._request())

        self.assertTrue(result.artifact_recorded)
        self.assertTrue(result.event_recorded)
        artifacts = self.store.list_task_artifacts("AT-HANDOFF-001")
        events = self.store.list_task_events("AT-HANDOFF-001")
        self.assertTrue(
            any(a.artifact_type == "pr_handoff" and a.path == result.json_path for a in artifacts)
        )
        self.assertTrue(any(e.event_type == "pr_handoff_created" for e in events))

    def test_dry_run_writes_no_files_artifacts_or_events(self) -> None:
        result = create_pr_handoff(self._request(dry_run=True))

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.json_path.exists())
        self.assertFalse(result.markdown_path.exists())
        self.assertFalse(result.artifact_recorded)
        self.assertFalse(result.event_recorded)
        artifacts = self.store.list_task_artifacts("AT-HANDOFF-001")
        events = self.store.list_task_events("AT-HANDOFF-001")
        self.assertFalse(any(a.artifact_type == "pr_handoff" for a in artifacts))
        self.assertFalse(any(e.event_type == "pr_handoff_created" for e in events))

    def test_rerun_refreshes_files_without_duplicate_artifact_records(self) -> None:
        first = create_pr_handoff(self._request())
        second = create_pr_handoff(self._request())

        self.assertTrue(first.artifact_recorded)
        self.assertFalse(second.artifact_recorded)
        artifacts = [
            a for a in self.store.list_task_artifacts("AT-HANDOFF-001")
            if a.artifact_type == "pr_handoff"
        ]
        events = [
            e for e in self.store.list_task_events("AT-HANDOFF-001")
            if e.event_type == "pr_handoff_created"
        ]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
