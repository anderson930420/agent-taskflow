from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord
from agent_taskflow.real_scheduled_execution_observability import (
    REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SCHEMA_VERSION,
    REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SOURCE,
    RealScheduledExecutionObservabilityRequest,
    summarize_real_scheduled_execution,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.github_issue_ingestion_failures import GitHubIssueIngestionFailureRegistry


def tick(
    *,
    status: str = "no_eligible_issues",
    ok: bool = True,
    mode: str = "confirmed",
    selected_task_key: str | None = None,
    selected_issue: dict[str, Any] | None = None,
    runner_config: dict[str, Any] | None = None,
    publication_config: dict[str, Any] | None = None,
    lock: dict[str, Any] | None = None,
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "schema_version": "github_issue_one_task_scheduler_tick.v1",
        "source": "github_issue_one_task_scheduler_tick",
        "status": status,
        "mode": mode,
        "repo": "anderson930420/agent-taskflow",
        "selected_task_key": selected_task_key,
        "lock": lock
        if lock is not None
        else {"acquired": True, "contended": False, "released": True},
    }
    if selected_issue is not None:
        payload["automation"] = {"selected_issue": selected_issue}
    if runner_config is not None:
        payload["runner_config"] = runner_config
    if publication_config is not None:
        payload["publication_config"] = publication_config
    if safety is not None:
        payload["safety"] = safety
    return payload


class RealScheduledExecutionObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_path = self.root / "tick.jsonl"
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_log(self, lines: list[str]) -> None:
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_ticks(self, ticks: list[dict[str, Any]]) -> None:
        self._write_log([json.dumps(t, sort_keys=True) for t in ticks])

    def _request(self, **overrides: Any) -> RealScheduledExecutionObservabilityRequest:
        values: dict[str, Any] = {"log_path": self.log_path}
        values.update(overrides)
        return RealScheduledExecutionObservabilityRequest(**values)

    def test_missing_log_returns_ok_with_no_last_tick_and_warning(self) -> None:
        result = summarize_real_scheduled_execution(self._request())

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["schema_version"],
            REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SCHEMA_VERSION,
        )
        self.assertEqual(
            result["source"], REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SOURCE
        )
        self.assertIsNone(result["last_tick"])
        self.assertTrue(
            any("not found" in warning for warning in result["warnings"]),
            msg=f"warnings: {result['warnings']!r}",
        )
        self.assertEqual(result["recent_ticks"]["total_parsed"], 0)

    def test_empty_log_returns_ok_with_no_last_tick(self) -> None:
        self.log_path.write_text("", encoding="utf-8")

        result = summarize_real_scheduled_execution(self._request())

        self.assertTrue(result["ok"])
        self.assertIsNone(result["last_tick"])
        self.assertEqual(result["recent_ticks"]["total_parsed"], 0)
        self.assertEqual(result["recent_ticks"]["malformed_line_count"], 0)

    def test_malformed_lines_are_skipped_and_counted(self) -> None:
        self._write_log(
            [
                json.dumps(tick(status="no_eligible_issues")),
                "{not valid json",
                "",
                "[1, 2, 3]",  # valid JSON but not an object
                json.dumps(tick(status="no_eligible_issues")),
            ]
        )

        result = summarize_real_scheduled_execution(self._request())

        recent = result["recent_ticks"]
        self.assertEqual(recent["total_parsed"], 2)
        self.assertEqual(recent["malformed_line_count"], 2)
        self.assertTrue(
            any("malformed" in warning for warning in result["warnings"])
        )

    def test_partially_written_last_line_is_tolerated(self) -> None:
        valid = json.dumps(tick(status="no_eligible_issues"))
        # Simulate a partially flushed final line (truncated JSON, no newline).
        self.log_path.write_text(valid + "\n" + valid[:20], encoding="utf-8")

        result = summarize_real_scheduled_execution(self._request())

        self.assertTrue(result["ok"])
        self.assertEqual(result["recent_ticks"]["total_parsed"], 1)
        self.assertEqual(result["recent_ticks"]["malformed_line_count"], 1)

    def test_latest_valid_tick_is_summarized(self) -> None:
        self._write_ticks(
            [
                tick(status="no_eligible_issues"),
                tick(
                    status="execution_completed",
                    ok=True,
                    mode="confirmed",
                    selected_task_key="AT-GH-123",
                    selected_issue={
                        "number": 123,
                        "title": "Do the thing",
                        "url": "https://github.com/x/y/issues/123",
                    },
                    runner_config={
                        "executor": "opencode",
                        "model": "minimax-coding-plan/MiniMax-M2.7",
                        "validators": ["policy"],
                        "worktree_root": "/home/ubuntu/agent-taskflow-cron/.worktrees",
                    },
                    publication_config={
                        "publish_after_execution": False,
                        "mode": "execution_only",
                    },
                    lock={"acquired": True, "contended": False, "released": True},
                    safety={"read_only": False, "human_review_required": True},
                ),
            ]
        )

        result = summarize_real_scheduled_execution(self._request())
        last = result["last_tick"]

        self.assertEqual(last["status"], "execution_completed")
        self.assertEqual(last["mode"], "confirmed")
        self.assertTrue(last["ok"])
        self.assertEqual(last["selected_task_key"], "AT-GH-123")
        self.assertEqual(last["selected_issue"]["number"], 123)
        self.assertEqual(last["selected_issue"]["title"], "Do the thing")
        self.assertEqual(
            last["selected_issue"]["url"], "https://github.com/x/y/issues/123"
        )
        self.assertEqual(last["runner_config"]["executor"], "opencode")
        self.assertEqual(
            last["runner_config"]["model"], "minimax-coding-plan/MiniMax-M2.7"
        )
        self.assertEqual(last["runner_config"]["validators"], ["policy"])
        self.assertEqual(
            last["runner_config"]["worktree_root"],
            "/home/ubuntu/agent-taskflow-cron/.worktrees",
        )
        self.assertFalse(last["publication_config"]["publish_after_execution"])
        self.assertEqual(last["publication_config"]["mode"], "execution_only")
        self.assertTrue(last["lock"]["acquired"])
        self.assertFalse(last["lock"]["contended"])
        self.assertTrue(last["lock"]["released"])
        self.assertEqual(last["safety"]["human_review_required"], True)

    def test_no_eligible_count_is_computed(self) -> None:
        self._write_ticks(
            [
                tick(status="no_eligible_issues"),
                tick(status="no_eligible_issues"),
                tick(status="execution_completed"),
            ]
        )

        result = summarize_real_scheduled_execution(self._request())

        recent = result["recent_ticks"]
        self.assertEqual(recent["total_parsed"], 3)
        self.assertEqual(recent["no_eligible_count"], 2)
        self.assertEqual(recent["ok_count"], 3)
        self.assertEqual(recent["failure_count"], 0)

    def test_execution_completed_count_is_computed(self) -> None:
        self._write_ticks(
            [
                tick(status="execution_completed"),
                tick(status="execution_completed"),
                tick(status="no_eligible_issues"),
            ]
        )

        result = summarize_real_scheduled_execution(self._request())

        self.assertEqual(
            result["recent_ticks"]["execution_completed_count"], 2
        )

    def test_lock_contention_count_is_computed(self) -> None:
        self._write_ticks(
            [
                tick(
                    status="locked",
                    ok=True,
                    lock={"acquired": False, "contended": True, "released": False},
                ),
                tick(
                    status="no_eligible_issues",
                    lock={"acquired": False, "contended": True, "released": False},
                ),
                tick(status="no_eligible_issues"),
            ]
        )

        result = summarize_real_scheduled_execution(self._request())

        # One tick has status=locked; one has lock.contended=True. Both count.
        self.assertEqual(result["recent_ticks"]["lock_contention_count"], 2)

    def test_recent_limit_windows_counts(self) -> None:
        self._write_ticks(
            [tick(status="execution_completed") for _ in range(5)]
            + [tick(status="no_eligible_issues") for _ in range(2)]
        )

        result = summarize_real_scheduled_execution(self._request(recent_limit=2))

        recent = result["recent_ticks"]
        self.assertEqual(recent["limit"], 2)
        self.assertEqual(recent["total_in_log"], 7)
        self.assertEqual(recent["total_parsed"], 2)
        self.assertEqual(recent["no_eligible_count"], 2)
        self.assertEqual(recent["execution_completed_count"], 0)

    def test_backlog_counts_from_store(self) -> None:
        self._write_ticks([tick(status="no_eligible_issues")])
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        for task_key, status in (
            ("AT-GH-1", "waiting_approval"),
            ("AT-GH-2", "waiting_approval"),
            ("AT-GH-3", "queued"),
        ):
            store.upsert_task(
                TaskRecord(
                    task_key=task_key,
                    project="agent-taskflow",
                    title=f"Task {task_key}",
                    status=status,
                    repo_path=self.repo,
                )
            )
        store.upsert_task(
            TaskRecord(
                task_key="AT-GH-4",
                project="agent-taskflow",
                title="Closed issue task",
                status="blocked",
                repo_path=self.repo,
                blocked_reason="GitHub issue is closed",
            )
        )

        result = summarize_real_scheduled_execution(
            self._request(db_path=self.db_path)
        )

        backlog = result["backlog"]
        self.assertTrue(backlog["available"])
        self.assertEqual(backlog["waiting_approval_count"], 2)
        self.assertEqual(backlog["blocked_count"], 1)
        self.assertEqual(backlog["queued_count"], 1)
        self.assertEqual(len(backlog["recent_waiting_approval"]), 2)
        blocked_keys = {item["task_key"] for item in backlog["recent_blocked"]}
        self.assertIn("AT-GH-4", blocked_keys)
        blocked_reason = backlog["recent_blocked"][0]["blocked_reason"]
        self.assertEqual(blocked_reason, "GitHub issue is closed")

    def test_ingestion_failure_registry_counts(self) -> None:
        self._write_ticks([tick(status="no_eligible_issues")])
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        # Issue 10 is recorded but not yet quarantined; issue 11 is
        # quarantined.
        GitHubIssueIngestionFailureRegistry(self.db_path).record_failure(
            repo="anderson930420/agent-taskflow",
            issue_number=10,
            error_summary="boom",
            quarantine_after_failures=2,
        )
        for _ in range(3):
            GitHubIssueIngestionFailureRegistry(self.db_path).record_failure(
                repo="anderson930420/agent-taskflow",
                issue_number=11,
                error_summary="boom",
            )

        result = summarize_real_scheduled_execution(
            self._request(db_path=self.db_path)
        )

        registry = result["ingestion_failure_registry"]
        self.assertTrue(registry["available"])
        self.assertEqual(registry["ingestion_failure_count"], 2)
        self.assertEqual(registry["quarantined_ingestion_failure_count"], 1)
        self.assertEqual(len(registry["records"]), 2)

    def test_missing_db_does_not_create_file_and_warns(self) -> None:
        self._write_ticks([tick(status="no_eligible_issues")])

        result = summarize_real_scheduled_execution(
            self._request(db_path=self.db_path)
        )

        self.assertFalse(self.db_path.exists())
        self.assertFalse(result["backlog"]["available"])
        self.assertFalse(result["ingestion_failure_registry"]["available"])
        self.assertTrue(
            any("state DB not found" in warning for warning in result["warnings"])
        )

    def test_safety_flags_prove_read_only_behavior(self) -> None:
        self._write_ticks([tick(status="no_eligible_issues")])

        result = summarize_real_scheduled_execution(self._request())

        safety = result["safety"]
        self.assertTrue(safety["read_only"])
        for flag in (
            "cron_modified",
            "db_written",
            "github_called",
            "executor_started",
            "validator_started",
            "issue_ingested",
            "branch_pushed",
            "draft_pr_created",
            "merged",
            "approved",
            "cleanup_performed",
            "branch_deleted",
            "worktree_deleted",
            "daemon_started",
            "scheduler_loop_started",
        ):
            self.assertIn(flag, safety)
            self.assertFalse(safety[flag], msg=f"{flag} must be False")

    def test_recent_limit_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            RealScheduledExecutionObservabilityRequest(
                log_path=self.log_path, recent_limit=0
            )

    def test_source_does_not_perform_automation(self) -> None:
        # Guard against code that would mutate state. Only executable tokens are
        # checked; the module docstring intentionally names actions it avoids.
        source = Path(
            "agent_taskflow/real_scheduled_execution_observability.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "while True",
            "import subprocess",
            "subprocess.",
            "os.system",
            "threading.Thread",
            "record_approval_decision(",
            "update_task_status(",
            "ingest_github_issue(",
            "merge_pull_request",
            "create_draft_pr",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
