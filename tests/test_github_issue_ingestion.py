from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_ingestion import (
    GitHubIssueIngestionError,
    GitHubIssueIngestionRequest,
    GitHubIssueSnapshot,
    ingest_github_issue,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


def open_issue() -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=42,
        title="Implement prepared workspace follow-up",
        body="Human-written issue body.\n\nAcceptance criteria here.",
        state="open",
        labels=("workflow", "github"),
        author="octocat",
        url="https://github.com/anderson930420/agent-taskflow/issues/42",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


def closed_issue() -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=43,
        title="Closed issue should not be runnable",
        body="Already closed.",
        state="closed",
        labels=(),
        author=None,
        url="https://github.com/anderson930420/agent-taskflow/issues/43",
        created_at=None,
        updated_at=None,
    )


class GitHubIssueIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(
        self,
        *,
        issue_number: int = 42,
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> GitHubIssueIngestionRequest:
        return GitHubIssueIngestionRequest(
            repo="anderson930420/agent-taskflow",
            issue_number=issue_number,
            local_repo_path=self.local_repo,
            artifact_root=self.root / "artifacts",
            task_key=task_key,
            dry_run=dry_run,
        )

    def test_ingest_open_issue_creates_queued_task(self) -> None:
        result = ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ingested")
        self.assertEqual(result.task_key, "AT-GH-42")
        self.assertTrue(result.wrote_task)
        task = self.store.get_task("AT-GH-42")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.title, "Implement prepared workspace follow-up")
        self.assertEqual(task.project, "agent-taskflow")
        self.assertEqual(task.artifact_dir, self.root / "artifacts" / "AT-GH-42")

    def test_ingest_writes_executor_profile_into_task_record(self) -> None:
        request = GitHubIssueIngestionRequest(
            repo="anderson930420/agent-taskflow",
            issue_number=42,
            local_repo_path=self.local_repo,
            artifact_root=self.root / "artifacts",
            model="claude-sonnet-4-6",
            provider="anthropic",
            tools=("read", "write", "read"),
            pi_bin="/usr/local/bin/pi",
        )

        result = ingest_github_issue(
            request,
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertTrue(result.ok)
        task = self.store.get_task("AT-GH-42")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.model, "claude-sonnet-4-6")
        self.assertEqual(task.provider, "anthropic")
        # Tools are normalized: stripped, de-duplicated, order preserved.
        self.assertEqual(task.tools, ["read", "write"])
        self.assertEqual(task.pi_bin, "/usr/local/bin/pi")
        # Ingestion records the profile but does not select an executor; the
        # default (noop) behavior is preserved.
        self.assertIsNone(task.executor)

    def test_ingest_without_profile_leaves_executor_fields_unset(self) -> None:
        ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        task = self.store.get_task("AT-GH-42")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertIsNone(task.executor)
        self.assertIsNone(task.model)
        self.assertIsNone(task.provider)
        self.assertIsNone(task.tools)
        self.assertIsNone(task.pi_bin)

    def test_ingest_writes_issue_spec_artifact(self) -> None:
        result = ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertTrue(result.issue_spec_path.is_file())
        text = result.issue_spec_path.read_text(encoding="utf-8")
        self.assertIn("# GitHub Issue Spec", text)
        self.assertIn("Issue number: 42", text)
        self.assertIn("Human-written issue body", text)
        self.assertIn("input/spec evidence", text)
        artifacts = self.store.list_task_artifacts("AT-GH-42")
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].artifact_type, "issue_spec")
        self.assertEqual(artifacts[0].path, result.issue_spec_path)

    def test_ingest_records_github_issue_ingested_event(self) -> None:
        ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        events = self.store.list_task_events("AT-GH-42")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "github_issue_ingested")
        self.assertEqual(events[0].source, "github")
        self.assertEqual(events[0].message, "GitHub issue ingested")
        payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(payload["repo"], "anderson930420/agent-taskflow")
        self.assertEqual(payload["issue_number"], 42)
        self.assertFalse(payload["dry_run"])

    def test_ingest_does_not_create_task_worktree_record(self) -> None:
        ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertIsNone(self.store.get_task_worktree("AT-GH-42"))

    def test_ingest_is_idempotent_and_reuses_existing_task(self) -> None:
        first = ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )
        second = ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertEqual(first.status, "ingested")
        self.assertEqual(second.status, "reused")
        self.assertFalse(second.wrote_task)
        tasks = self.store.list_tasks()
        self.assertEqual([task.task_key for task in tasks], ["AT-GH-42"])
        self.assertEqual(len(self.store.list_task_artifacts("AT-GH-42")), 1)
        self.assertEqual(len(self.store.list_task_events("AT-GH-42")), 2)

    def test_reingest_preserves_existing_active_status(self) -> None:
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-GH-42",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Old title",
                status="waiting_approval",
                repo_path=self.local_repo,
                artifact_dir=self.root / "old-artifacts" / "AT-GH-42",
            )
        )

        result = ingest_github_issue(
            self.request(),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertEqual(result.status, "reused")
        self.assertFalse(result.wrote_task)
        task = self.store.get_task("AT-GH-42")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(task.title, "Implement prepared workspace follow-up")
        self.assertEqual(task.artifact_dir, self.root / "artifacts" / "AT-GH-42")

    def test_dry_run_writes_nothing(self) -> None:
        result = ingest_github_issue(
            self.request(dry_run=True),
            store=self.store,
            fetcher=lambda repo, issue_number: open_issue(),
        )

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.wrote_task)
        self.assertFalse(result.wrote_artifact)
        self.assertFalse(result.recorded_event)
        self.assertFalse(self.db_path.exists())
        self.assertFalse(result.issue_spec_path.exists())

    def test_closed_issue_maps_to_blocked(self) -> None:
        result = ingest_github_issue(
            self.request(issue_number=43),
            store=self.store,
            fetcher=lambda repo, issue_number: closed_issue(),
        )

        self.assertEqual(result.status, "ingested")
        task = self.store.get_task("AT-GH-43")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertIn("closed", task.blocked_reason or "")

    def test_missing_body_still_writes_spec(self) -> None:
        issue = GitHubIssueSnapshot(
            number=44,
            title="No body issue",
            body="",
            state="open",
            labels=(),
            author=None,
            url=None,
            created_at=None,
            updated_at=None,
        )

        result = ingest_github_issue(
            self.request(issue_number=44),
            store=self.store,
            fetcher=lambda repo, issue_number: issue,
        )

        text = result.issue_spec_path.read_text(encoding="utf-8")
        self.assertIn("(empty)", text)

    def test_invalid_local_repo_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "existing directory"):
            GitHubIssueIngestionRequest(
                repo="anderson930420/agent-taskflow",
                issue_number=42,
                local_repo_path=self.root / "missing",
            )

    def test_fetch_failure_blocks_ingestion(self) -> None:
        def failing_fetcher(repo: str, issue_number: int) -> GitHubIssueSnapshot:
            raise GitHubIssueIngestionError("not found")

        with self.assertRaisesRegex(GitHubIssueIngestionError, "not found"):
            ingest_github_issue(
                self.request(),
                store=self.store,
                fetcher=failing_fetcher,
            )

        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
