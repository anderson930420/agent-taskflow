from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_ingestion import (
    GitHubIssueIngestionRequest,
    GitHubIssueSnapshot,
    ingest_github_issue,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


def open_issue() -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=42,
        title="Open issue title from GitHub",
        body="Open issue body.",
        state="open",
        labels=("automation",),
        author="octocat",
        url="https://github.com/anderson930420/agent-taskflow/issues/42",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


def closed_issue() -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=43,
        title="Closed issue title from GitHub",
        body="Closed issue body.",
        state="closed",
        labels=(),
        author="octocat",
        url="https://github.com/anderson930420/agent-taskflow/issues/43",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-03T00:00:00Z",
    )


class GitHubIssueIngestionStatusHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(
        self,
        *,
        issue_number: int = 42,
        task_key: str | None = None,
    ) -> GitHubIssueIngestionRequest:
        return GitHubIssueIngestionRequest(
            repo="anderson930420/agent-taskflow",
            issue_number=issue_number,
            local_repo_path=self.local_repo,
            artifact_root=self.root / "artifacts",
            task_key=task_key,
        )

    def seed_task(self, *, task_key: str, status: str) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Existing local task title",
                status=status,
                repo_path=self.local_repo,
                artifact_dir=self.root / "old-artifacts" / task_key,
            )
        )

    def test_reingest_preserves_each_in_progress_status(self) -> None:
        active_statuses = (
            "preparing",
            "implementing",
            "validating",
            "waiting_approval",
        )

        for index, status in enumerate(active_statuses, start=1):
            with self.subTest(status=status):
                task_key = f"AT-GH-ACTIVE-{index}"
                self.seed_task(task_key=task_key, status=status)

                result = ingest_github_issue(
                    self.request(task_key=task_key),
                    store=self.store,
                    fetcher=lambda repo, issue_number: open_issue(),
                )

                self.assertEqual(result.status, "reused")
                self.assertFalse(result.wrote_task)
                self.assertTrue(result.wrote_artifact)
                self.assertTrue(result.recorded_event)
                task = self.store.get_task(task_key)
                self.assertIsNotNone(task)
                assert task is not None
                self.assertEqual(task.status, status)
                self.assertEqual(task.title, "Open issue title from GitHub")
                self.assertEqual(task.artifact_dir, self.root / "artifacts" / task_key)

    def test_closed_issue_reingest_does_not_block_active_task(self) -> None:
        self.seed_task(task_key="AT-GH-43", status="implementing")

        result = ingest_github_issue(
            self.request(issue_number=43),
            store=self.store,
            fetcher=lambda repo, issue_number: closed_issue(),
        )

        self.assertEqual(result.status, "reused")
        self.assertFalse(result.wrote_task)
        self.assertTrue(result.wrote_artifact)
        self.assertTrue(result.recorded_event)
        task = self.store.get_task("AT-GH-43")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "implementing")
        self.assertIsNone(task.blocked_reason)
        self.assertEqual(task.title, "Closed issue title from GitHub")
        events = self.store.list_task_events("AT-GH-43")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(payload["issue_state"], "closed")

    def test_closed_issue_still_blocks_new_task(self) -> None:
        result = ingest_github_issue(
            self.request(issue_number=43),
            store=self.store,
            fetcher=lambda repo, issue_number: closed_issue(),
        )

        self.assertEqual(result.status, "ingested")
        self.assertTrue(result.wrote_task)
        task = self.store.get_task("AT-GH-43")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertIn("closed", task.blocked_reason or "")


if __name__ == "__main__":
    unittest.main()
