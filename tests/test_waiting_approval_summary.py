from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.codex_advisory_review import (
    CodexAdvisoryReviewRequest,
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    generate_codex_advisory_review,
)
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.waiting_approval_summary import (
    WaitingApprovalSummaryRequest,
    summarize_waiting_approval_task,
    summarize_waiting_approval_task_markdown,
)


class WaitingApprovalSummaryTests(unittest.TestCase):
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

    def _task_key(self, suffix: str = "123") -> str:
        return f"AT-GH-{suffix}"

    def _issue_snapshot(self, issue_number: int = 123) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=issue_number,
            title="Implement waiting approval summary",
            body="Task body",
            state="open",
            labels=("ready", "summary"),
            author="octocat",
            url=f"https://github.com/anderson930420/agent-taskflow/issues/{issue_number}",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_task(
        self,
        *,
        task_key: str | None = None,
        status: str = "waiting_approval",
        with_issue_spec: bool = True,
        with_worktree: bool = True,
        executor_status: str | None = "completed",
        validator_status: str | None = "passed",
        with_approval: bool = False,
    ) -> Path:
        task_key = task_key or self._task_key()
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)

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

        if with_worktree:
            worktree_path = self.worktree_root / task_key
            worktree_path.mkdir(parents=True, exist_ok=True)
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

        if with_issue_spec:
            issue = self._issue_snapshot()
            issue_spec_path = artifact_dir / "issue_spec.md"
            issue_spec_path.write_text(
                render_issue_spec(
                    repo="anderson930420/agent-taskflow",
                    task_key=task_key,
                    issue=issue,
                    ingested_at="2026-05-03T00:00:00Z",
                ),
                encoding="utf-8",
            )
            self.store.record_task_artifact(task_key, "issue_spec", issue_spec_path)

        if executor_status is not None:
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
                status=executor_status,
                exit_code=0 if executor_status in {"completed", "passed"} else 1,
                summary="executor summary",
                log_path=executor_log,
                artifacts={"log": executor_log},
            )
            self.store.record_task_artifact(task_key, "worker_log", executor_log)

        if validator_status is not None:
            validator_log = artifact_dir / "pytest.log"
            validator_log.write_text("validator log\n", encoding="utf-8")
            self.store.record_validation_result(
                task_key,
                "pytest",
                status=validator_status,
                exit_code=0 if validator_status in {"passed", "completed"} else 1,
                summary="validator summary",
                log_path=validator_log,
                artifacts={"log": validator_log},
            )
            self.store.record_task_artifact(task_key, "review_log", validator_log)

        if with_approval:
            self.store.record_approval_decision(
                task_key,
                "accepted",
                decided_by="human",
                notes="Looks good",
            )

        return artifact_dir

    def _summarize(self, task_key: str, *, allow_non_waiting: bool = False, artifact_root: Path | None = None):
        request = WaitingApprovalSummaryRequest(
            task_key=task_key,
            db_path=self.db_path,
            artifact_root=artifact_root,
            allow_non_waiting=allow_non_waiting,
        )
        return summarize_waiting_approval_task(request)

    def test_missing_task_returns_not_found_result_without_db_write(self) -> None:
        before_size = self.db_path.stat().st_size
        result = self._summarize("AT-GH-999")
        after_size = self.db_path.stat().st_size

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertIn("Task not found", result.error or "")
        self.assertEqual(before_size, after_size)

    def test_non_waiting_task_is_rejected_by_default(self) -> None:
        self._seed_task(status="blocked")

        result = self._summarize(self._task_key())

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("waiting_approval", " ".join(result.review_readiness["blocking_warnings"]))

    def test_allow_non_waiting_can_summarize_other_statuses(self) -> None:
        self._seed_task(status="blocked")

        result = self._summarize(self._task_key(), allow_non_waiting=True)

        self.assertTrue(result.ok)
        self.assertEqual(result.task["status"], "blocked")
        self.assertEqual(result.status, "ok")
        self.assertIn("Task status is blocked", " ".join(result.warnings))

    def test_complete_evidence_is_ready_for_human_review(self) -> None:
        self._seed_task(with_approval=True)

        result = self._summarize(self._task_key())

        self.assertTrue(result.ok)
        self.assertTrue(result.review_readiness["ready_for_human_review"])
        self.assertTrue(result.source["available"])
        self.assertTrue(result.workspace["available"])
        self.assertTrue(result.executor["finished_ok"])
        self.assertTrue(result.validators["all_passed"])
        self.assertTrue(result.approval_review["available"])

    def test_missing_issue_spec_creates_blocking_warning(self) -> None:
        self._seed_task(with_issue_spec=False)

        result = self._summarize(self._task_key())

        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("Issue/spec evidence is missing", result.review_readiness["blocking_warnings"])

    def test_missing_worktree_creates_blocking_warning(self) -> None:
        self._seed_task(with_worktree=False)

        result = self._summarize(self._task_key())

        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("Worktree evidence is missing", result.review_readiness["blocking_warnings"])

    def test_missing_executor_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(executor_status=None)

        result = self._summarize(self._task_key())

        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("Executor evidence is missing", result.review_readiness["blocking_warnings"])

    def test_failed_executor_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(executor_status="failed")

        result = self._summarize(self._task_key())

        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("Executor did not finish successfully", result.review_readiness["blocking_warnings"])

    def test_missing_validator_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(validator_status=None)

        result = self._summarize(self._task_key())

        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("Validator evidence is missing", result.review_readiness["blocking_warnings"])

    def test_failed_validator_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(validator_status="failed")

        result = self._summarize(self._task_key())

        self.assertFalse(result.review_readiness["ready_for_human_review"])
        self.assertIn("At least one validator failed or was blocked", result.review_readiness["blocking_warnings"])

    def test_artifacts_are_listed_deterministically(self) -> None:
        artifact_dir = self._seed_task(with_approval=True)
        extra_a = artifact_dir / "z-extra.log"
        extra_b = artifact_dir / "a-extra.log"
        extra_a.write_text("z\n", encoding="utf-8")
        extra_b.write_text("a\n", encoding="utf-8")
        self.store.record_task_artifact(self._task_key(), "branch_push", extra_a)
        self.store.record_task_artifact(self._task_key(), "draft_pr", extra_b)

        result = self._summarize(self._task_key())
        ordered = [(item["kind"], item["path"]) for item in result.artifacts]

        self.assertEqual(ordered, sorted(ordered))

    def test_safety_block_is_explicit_and_read_only(self) -> None:
        self._seed_task()

        result = self._summarize(self._task_key())

        self.assertTrue(result.safety["read_only"])
        self.assertFalse(result.safety["task_status_changed"])
        self.assertFalse(result.safety["db_written"])
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])
        self.assertFalse(result.safety["validators_started"])
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])
        self.assertFalse(result.safety["background_worker_started"])

    def test_codex_advisory_review_absent_is_backward_compatible(self) -> None:
        self._seed_task(with_approval=True)

        result = self._summarize(self._task_key())

        # New section is always present; absence does not break readiness.
        self.assertIn("present", result.codex_advisory_review)
        self.assertFalse(result.codex_advisory_review["present"])
        self.assertEqual(result.codex_advisory_review["review_status"], "missing")
        self.assertFalse(result.codex_advisory_review["validation_authority"])
        self.assertTrue(result.codex_advisory_review["human_review_required"])
        self.assertTrue(result.review_readiness["ready_for_human_review"])

    def test_codex_advisory_review_section_is_surfaced_when_present(self) -> None:
        artifact_dir = self._seed_task(with_approval=True)
        generate_codex_advisory_review(
            CodexAdvisoryReviewRequest(
                task_key=self._task_key(), artifact_dir=artifact_dir
            )
        )

        result, markdown = summarize_waiting_approval_task_markdown(
            WaitingApprovalSummaryRequest(
                task_key=self._task_key(), db_path=self.db_path
            )
        )

        codex = result.codex_advisory_review
        self.assertTrue(codex["present"])
        self.assertEqual(codex["review_status"], "not_run")
        self.assertEqual(codex["risk_level"], "unknown")
        self.assertFalse(codex["validation_authority"])
        self.assertTrue(codex["human_review_required"])
        self.assertEqual(codex["json_path"], str(artifact_dir / JSON_FILENAME))
        self.assertEqual(codex["markdown_path"], str(artifact_dir / MARKDOWN_FILENAME))
        self.assertIn("Codex Advisory Review", markdown)

    def test_codex_advisory_status_does_not_change_authority(self) -> None:
        # A high_risk advisory artifact must not block readiness, change the
        # validator result, the lifecycle status, the approval authority, or any
        # execution-allowed signal.
        artifact_dir = self._seed_task(with_approval=True)
        baseline = self._summarize(self._task_key())

        (artifact_dir / JSON_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": "codex_advisory_review.v1",
                    "reviewer": "codex-cli",
                    "task_key": self._task_key(),
                    "review_status": "high_risk",
                    "risk_level": "high",
                    "validation_authority": False,
                    "human_review_required": True,
                    "summary": "advisory only",
                    "confirm_run": True,
                    "codex_cli_invoked": True,
                    "tool_error": None,
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )
        (artifact_dir / MARKDOWN_FILENAME).write_text("# md\n", encoding="utf-8")

        result = self._summarize(self._task_key())

        self.assertEqual(result.codex_advisory_review["review_status"], "high_risk")
        # Authority and readiness are unchanged versus the no-Codex baseline.
        self.assertEqual(
            result.review_readiness["ready_for_human_review"],
            baseline.review_readiness["ready_for_human_review"],
        )
        self.assertTrue(result.review_readiness["ready_for_human_review"])
        self.assertEqual(result.validators["all_passed"], baseline.validators["all_passed"])
        self.assertEqual(result.task["status"], "waiting_approval")
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["task_status_changed"])
        self.assertFalse(result.codex_advisory_review["validation_authority"])

    def test_summary_does_not_write_db_or_change_status(self) -> None:
        self._seed_task(with_approval=True)
        before_task = self.store.get_task(self._task_key())
        before_events = len(self.store.list_task_events(self._task_key()))
        before_artifacts = len(self.store.list_task_artifacts(self._task_key()))
        before_mtime = self.db_path.stat().st_mtime_ns

        result = self._summarize(self._task_key())

        after_task = self.store.get_task(self._task_key())
        after_events = len(self.store.list_task_events(self._task_key()))
        after_artifacts = len(self.store.list_task_artifacts(self._task_key()))
        after_mtime = self.db_path.stat().st_mtime_ns

        self.assertTrue(result.ok)
        self.assertEqual(before_task.status, after_task.status)
        self.assertEqual(before_events, after_events)
        self.assertEqual(before_artifacts, after_artifacts)
        self.assertEqual(before_mtime, after_mtime)


if __name__ == "__main__":
    unittest.main()
