from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_discovery import read_local_issue_matches
from agent_taskflow.github_issue_intake import (
    GitHubIssueIntakeRequest,
    GitHubIssueSnapshot,
    intake_selected_github_issues,
)
from agent_taskflow.models import TaskRecord
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


class GitHubIssueIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, *issue_numbers: int, dry_run: bool = False) -> GitHubIssueIntakeRequest:
        return GitHubIssueIntakeRequest(
            repo="anderson930420/agent-taskflow",
            issue_numbers=tuple(issue_numbers),
            db_path=self.db_path,
            local_repo_path=self.repo,
            artifact_root=self.root / "artifacts",
            dry_run=dry_run,
        )

    def ingest(self, snapshots: dict[int, GitHubIssueSnapshot], *issue_numbers: int, dry_run: bool = False):
        return intake_selected_github_issues(
            self.request(*issue_numbers, dry_run=dry_run),
            store=self.store,
            fetcher=lambda repo, issue_number: snapshots[issue_number],
        )

    def test_selected_open_issue_creates_queued_task(self) -> None:
        payload = self.ingest({123: issue(123)}, 123)

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["ingested"][0]["task_key"], "AT-GH-123")
        task = self.store.get_task("AT-GH-123")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

    def test_selected_open_issue_writes_issue_spec_artifact(self) -> None:
        payload = self.ingest({124: issue(124, title="Artifact issue")}, 124)

        entry = payload["ingested"][0]
        artifact_path = Path(entry["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        text = artifact_path.read_text(encoding="utf-8")
        self.assertIn("Artifact issue", text)
        self.assertIn("Issue number: 124", text)
        self.assertEqual(self.store.list_task_artifacts("AT-GH-124")[0].artifact_type, "issue_spec")

    def test_selected_open_issue_records_ingestion_event(self) -> None:
        payload = self.ingest({125: issue(125)}, 125)

        self.assertEqual(payload["ingested"][0]["event_type"], "github_issue_ingested")
        events = self.store.list_task_events("AT-GH-125")
        self.assertEqual(len(events), 1)
        event_payload = json.loads(events[0].payload_json or "{}")
        self.assertTrue(event_payload["selected_intake"])
        self.assertEqual(event_payload["issue_number"], 125)

    def test_already_ingested_issue_does_not_duplicate_task_artifact_or_event(self) -> None:
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-GH-126",
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.root / "artifacts" / "AT-GH-126",
                title="Already ingested",
            )
        )
        self.store.record_task_artifact("AT-GH-126", "issue_spec", self.root / "artifacts" / "AT-GH-126" / "issue_spec.md")
        self.store.record_task_event(
            "AT-GH-126",
            "github_issue_ingested",
            "github",
            message="GitHub issue ingested",
            payload={"kind": "github_issue_ingested", "repo": "anderson930420/agent-taskflow", "issue_number": 126},
        )

        before_artifacts = len(self.store.list_task_artifacts("AT-GH-126"))
        before_events = len(self.store.list_task_events("AT-GH-126"))
        payload = self.ingest({126: issue(126)}, 126)

        self.assertEqual(payload["already_ingested"][0]["task_key"], "AT-GH-126")
        self.assertEqual(len(self.store.list_task_artifacts("AT-GH-126")), before_artifacts)
        self.assertEqual(len(self.store.list_task_events("AT-GH-126")), before_events)

    def test_closed_issue_is_not_eligible_and_does_not_write_db(self) -> None:
        payload = self.ingest({127: issue(127, state="closed")}, 127)

        self.assertEqual(payload["not_eligible"][0]["reason"], "issue state is not open")
        self.assertFalse(self.db_path.exists())

    def test_blocked_label_issue_is_not_eligible_and_does_not_write_db(self) -> None:
        payload = self.ingest({128: issue(128, labels=("workflow", "blocked"))}, 128)

        self.assertEqual(payload["not_eligible"][0]["blocked_labels"], ["blocked"])
        self.assertFalse(self.db_path.exists())

    def test_summary_counts_are_deterministic(self) -> None:
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-GH-129",
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.root / "artifacts" / "AT-GH-129",
                title="Existing",
            )
        )
        self.store.record_task_event(
            "AT-GH-129",
            "github_issue_ingested",
            "github",
            message="GitHub issue ingested",
            payload={"kind": "github_issue_ingested", "repo": "anderson930420/agent-taskflow", "issue_number": 129},
        )

        payload = self.ingest(
            {
                130: issue(130),
                129: issue(129),
                131: issue(131, state="closed"),
                132: issue(132, labels=("no-agent",)),
            },
            130,
            129,
            131,
            132,
        )

        self.assertEqual(
            payload["summary"],
            {
                "selected_count": 4,
                "ingested_count": 1,
                "already_ingested_count": 1,
                "not_eligible_count": 2,
                "failed_count": 0,
            },
        )

    def test_safety_block_explicitly_allows_only_selected_local_writes(self) -> None:
        payload = self.ingest({133: issue(133)}, 133)

        safety = payload["safety"]
        self.assertFalse(safety["read_only"])
        self.assertTrue(safety["selected_intake_only"])
        self.assertTrue(safety["db_written"])
        self.assertTrue(safety["artifact_written"])
        self.assertTrue(safety["event_recorded"])
        self.assertFalse(safety["workspace_prepared"])
        self.assertFalse(safety["executor_started"])
        self.assertFalse(safety["validators_started"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["pr_created"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["cleanup_performed"])

    def test_dry_run_does_not_write_db_or_artifacts(self) -> None:
        payload = self.ingest({134: issue(134)}, 134, dry_run=True)

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(self.db_path.exists())
        self.assertFalse(payload["safety"]["db_written"])
        self.assertFalse(payload["safety"]["artifact_written"])
        self.assertFalse(payload["safety"]["event_recorded"])

    def test_read_local_issue_matches_uses_existing_canonical_metadata(self) -> None:
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-GH-135",
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.root / "artifacts" / "AT-GH-135",
                title="Existing",
            )
        )
        self.store.record_task_event(
            "AT-GH-135",
            "github_issue_ingested",
            "github",
            message="GitHub issue ingested",
            payload={"kind": "github_issue_ingested", "repo": "anderson930420/agent-taskflow", "issue_number": 135},
        )

        matches = read_local_issue_matches(self.db_path, repo="anderson930420/agent-taskflow")
        self.assertEqual(matches[135].task_key, "AT-GH-135")


if __name__ == "__main__":
    unittest.main()
