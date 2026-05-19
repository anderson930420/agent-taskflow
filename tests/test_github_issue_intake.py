from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot
from agent_taskflow.github_issue_intake_gate import (
    GitHubIssueIntakeRequest,
    intake_selected_github_issues,
)
from agent_taskflow.store import TaskMirrorStore


def issue(
    number: int,
    *,
    title: str | None = None,
    state: str = "open",
    labels: tuple[str, ...] = (),
    body: str = "Issue body",
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=number,
        title=title or f"Issue {number}",
        body=body,
        state=state,
        labels=labels,
        author="octocat",
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


class GitHubIssueIntakeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, *issue_numbers: int, dry_run: bool = True) -> GitHubIssueIntakeRequest:
        return GitHubIssueIntakeRequest(
            repo="anderson930420/agent-taskflow",
            issue_numbers=tuple(issue_numbers),
            repo_path=self.repo,
            artifact_root=self.root / "artifacts",
            db_path=self.db_path,
            dry_run=dry_run,
        )

    def test_dry_run_does_not_write_to_db(self) -> None:
        result = intake_selected_github_issues(
            self.request(123, dry_run=True),
            store=self.store,
            fetcher=lambda repo, issue_number: issue(issue_number),
        )

        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["written"])
        self.assertEqual(result["selected"][0]["action"], "would_ingest")
        self.assertFalse(self.db_path.exists())

    def test_confirmed_intake_writes_queued_task_record(self) -> None:
        result = intake_selected_github_issues(
            self.request(123, dry_run=False),
            store=self.store,
            fetcher=lambda repo, issue_number: issue(issue_number, title="Queued task"),
        )

        self.assertEqual(result["mode"], "confirmed")
        self.assertTrue(result["written"])
        self.assertEqual(result["selected"][0]["action"], "ingested")

        task = self.store.get_task("GH-123")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.project, "agent-taskflow")
        self.assertEqual(task.board, "agent-taskflow")
        self.assertEqual(task.artifact_dir, self.root / "artifacts" / "GH-123")
        self.assertIsNone(self.store.get_task_worktree("GH-123"))

    def test_confirmed_intake_records_github_issue_ingested_event(self) -> None:
        intake_selected_github_issues(
            self.request(124, dry_run=False),
            store=self.store,
            fetcher=lambda repo, issue_number: issue(issue_number, title="Event task"),
        )

        events = self.store.list_task_events("GH-124")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "github_issue_ingested")
        self.assertEqual(events[0].source, "github_issue_intake")
        self.assertEqual(events[0].message, "GitHub issue ingested")
        payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(
            payload,
            {
                "kind": "github_issue_ingested",
                "repo": "anderson930420/agent-taskflow",
                "issue_number": 124,
                "issue_url": "https://github.com/anderson930420/agent-taskflow/issues/124",
                "status": "queued",
                "task_key": "GH-124",
                "title": "Event task",
            },
        )

    def test_confirmed_duplicate_issue_is_idempotent(self) -> None:
        first = intake_selected_github_issues(
            self.request(125, dry_run=False),
            store=self.store,
            fetcher=lambda repo, issue_number: issue(issue_number, title="Duplicate task"),
        )
        second = intake_selected_github_issues(
            self.request(125, dry_run=False),
            store=self.store,
            fetcher=lambda repo, issue_number: issue(issue_number, title="Duplicate task"),
        )

        self.assertEqual(first["selected"][0]["action"], "ingested")
        self.assertEqual(second["selected"][0]["action"], "already_ingested")
        self.assertFalse(second["written"])
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertEqual(len(self.store.list_task_events("GH-125")), 1)

    def test_fetcher_failure_marks_selected_entry_failed_and_blocks_ok(self) -> None:
        result = intake_selected_github_issues(
            self.request(128, dry_run=False),
            store=self.store,
            fetcher=lambda repo, issue_number: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["written"])
        self.assertEqual(result["summary"]["failed_count"], 1)
        self.assertEqual(result["selected"][0]["action"], "failed")
        self.assertEqual(result["selected"][0]["error"], "boom")
        self.assertFalse(self.db_path.exists())

    def test_invalid_non_absolute_repo_or_artifact_paths_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_path must be absolute"):
            GitHubIssueIntakeRequest(
                repo="anderson930420/agent-taskflow",
                issue_numbers=(126,),
                repo_path=Path("relative/repo"),
                artifact_root=self.root / "artifacts",
            )

        with self.assertRaisesRegex(ValueError, "artifact_root must be absolute"):
            GitHubIssueIntakeRequest(
                repo="anderson930420/agent-taskflow",
                issue_numbers=(126,),
                repo_path=self.repo,
                artifact_root=Path("relative/artifacts"),
            )

    def test_request_defaults_project_and_board_to_repo_name(self) -> None:
        request = GitHubIssueIntakeRequest(
            repo="anderson930420/agent-taskflow",
            issue_numbers=(127,),
            repo_path=self.repo,
            artifact_root=self.root / "artifacts",
        )

        self.assertEqual(request.project, "agent-taskflow")
        self.assertEqual(request.board, "agent-taskflow")
        self.assertTrue(request.dry_run)


if __name__ == "__main__":
    unittest.main()
