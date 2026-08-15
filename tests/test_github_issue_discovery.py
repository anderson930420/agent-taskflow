from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_discovery import (
    GitHubIssueDiscoveryIssue,
    GitHubIssueDiscoveryRequest,
    discover_github_issues,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


def issue(
    number: int,
    *,
    title: str | None = None,
    state: str = "open",
    labels: tuple[str, ...] = (),
) -> GitHubIssueDiscoveryIssue:
    return GitHubIssueDiscoveryIssue(
        number=number,
        title=title or f"Issue {number}",
        state=state,
        labels=labels,
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


class GitHubIssueDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **overrides: object) -> GitHubIssueDiscoveryRequest:
        values = {
            "repo": "anderson930420/agent-taskflow",
            "db_path": self.db_path,
        }
        values.update(overrides)
        return GitHubIssueDiscoveryRequest(**values)

    def discover(self, issues: list[GitHubIssueDiscoveryIssue], **overrides: object) -> dict[str, object]:
        return discover_github_issues(
            self.request(**overrides),
            fetcher=lambda request: issues,
        )

    def add_ingested_task(self, *, issue_number: int, task_key: str | None = None) -> None:
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        key = task_key or f"AT-GH-{issue_number}"
        store.upsert_task(
            TaskRecord(
                task_key=key,
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
                title=f"Existing {issue_number}",
            )
        )
        store.record_task_event(
            key,
            "github_issue_ingested",
            "github",
            message="GitHub issue ingested",
            payload={
                "kind": "github_issue_ingested",
                "repo": "anderson930420/agent-taskflow",
                "issue_number": issue_number,
            },
        )

    def add_stale_ingestion_residue(self, *, issue_number: int) -> None:
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        task_key = f"AT-GH-{issue_number}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_events (
                    task_key, event_type, source, message, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_key,
                    "github_issue_ingested",
                    "github",
                    "stale ingestion residue",
                    json.dumps(
                        {
                            "repo": "anderson930420/agent-taskflow",
                            "issue_number": issue_number,
                        }
                    ),
                    "2026-08-14T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO task_artifacts (task_key, artifact_type, path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_key,
                    "issue_spec",
                    f"/stale/{task_key}/issue_spec.md",
                    "2026-08-14T00:00:00Z",
                ),
            )

    def test_open_issue_not_in_local_mirror_is_new_and_recommended(self) -> None:
        payload = self.discover([issue(123, title="New operator issue")])

        self.assertEqual(payload["status"], "discovered")
        self.assertEqual([item["number"] for item in payload["new_issues"]], [123])
        self.assertEqual(
            [item["number"] for item in payload["recommended_candidates"]],
            [123],
        )
        self.assertEqual(
            payload["new_issues"][0]["reason"],
            "open issue not found in local task mirror",
        )
        self.assertFalse(self.db_path.exists())

    def test_already_ingested_issue_is_not_recommended(self) -> None:
        self.add_ingested_task(issue_number=100, task_key="CUSTOM-100")

        payload = self.discover([issue(100, title="Already ingested issue")])

        self.assertEqual(payload["new_issues"], [])
        self.assertEqual(payload["recommended_candidates"], [])
        self.assertEqual(payload["already_ingested"][0]["number"], 100)
        self.assertEqual(payload["already_ingested"][0]["task_key"], "CUSTOM-100")

    def test_force_reingest_bypasses_stale_residue_without_task_row(self) -> None:
        self.add_stale_ingestion_residue(issue_number=120)

        default_payload = self.discover([issue(120, title="Stale residue issue")])
        explicit_empty_payload = self.discover(
            [issue(120, title="Stale residue issue")],
            force_reingest_issue_numbers=(),
        )
        forced_payload = self.discover(
            [issue(120, title="Stale residue issue")],
            force_reingest_issue_numbers=(120, 120),
        )

        self.assertEqual(default_payload, explicit_empty_payload)
        self.assertNotIn("force_reingest_issue_numbers", default_payload)
        self.assertEqual(default_payload["recommended_candidates"], [])
        self.assertEqual(default_payload["already_ingested"][0]["task_key"], "AT-GH-120")

        self.assertEqual(forced_payload["force_reingest_issue_numbers"], [120])
        self.assertEqual(forced_payload["already_ingested"], [])
        self.assertEqual(
            [item["number"] for item in forced_payload["recommended_candidates"]],
            [120],
        )
        candidate = forced_payload["recommended_candidates"][0]
        self.assertTrue(candidate["force_reingest_issue"])
        self.assertEqual(
            candidate["reason"],
            "open issue explicitly selected for force re-ingestion",
        )

    def test_force_reingest_still_enforces_label_filters(self) -> None:
        self.add_stale_ingestion_residue(issue_number=121)

        payload = self.discover(
            [issue(121, labels=("needs-triage",))],
            force_reingest_issue_numbers=(121,),
            include_labels=("ready",),
        )

        self.assertEqual(payload["recommended_candidates"], [])
        self.assertEqual(payload["already_ingested"], [])
        self.assertEqual(payload["not_eligible"][0]["number"], 121)
        self.assertEqual(payload["not_eligible"][0]["missing_labels"], ["ready"])

    def test_closed_issue_is_closed_or_blocked_not_recommended(self) -> None:
        payload = self.discover([issue(124, state="closed")])

        self.assertEqual(payload["new_issues"], [])
        self.assertEqual(payload["recommended_candidates"], [])
        self.assertEqual(payload["closed_or_blocked"][0]["number"], 124)
        self.assertEqual(payload["closed_or_blocked"][0]["reason"], "issue state is not open")

    def test_blocked_label_issue_is_closed_or_blocked_not_recommended(self) -> None:
        payload = self.discover([issue(125, labels=("workflow", "blocked"))])

        self.assertEqual(payload["new_issues"], [])
        self.assertEqual(payload["recommended_candidates"], [])
        self.assertEqual(payload["closed_or_blocked"][0]["number"], 125)
        self.assertEqual(payload["closed_or_blocked"][0]["blocked_labels"], ["blocked"])

    def test_include_and_exclude_labels_make_issue_not_eligible(self) -> None:
        missing = self.discover([issue(126, labels=("workflow",))], include_labels=("ready",))
        excluded = self.discover([issue(127, labels=("workflow", "skip"))], exclude_labels=("skip",))

        self.assertEqual(missing["not_eligible"][0]["missing_labels"], ["ready"])
        self.assertEqual(excluded["not_eligible"][0]["excluded_labels"], ["skip"])
        self.assertEqual(missing["recommended_candidates"], [])
        self.assertEqual(excluded["recommended_candidates"], [])

    def test_summary_counts_are_correct(self) -> None:
        self.add_ingested_task(issue_number=130)
        payload = self.discover(
            [
                issue(129, labels=("ready",)),
                issue(130),
                issue(131, state="closed"),
                issue(132, labels=("no-agent",)),
                issue(133, labels=("needs-triage",)),
            ],
            include_labels=("ready",),
        )

        self.assertEqual(
            payload["summary"],
            {
                "new_issue_count": 1,
                "already_ingested_count": 1,
                "closed_or_blocked_count": 2,
                "not_eligible_count": 1,
                "recommended_candidate_count": 1,
            },
        )

    def test_safety_block_is_explicit_read_only(self) -> None:
        payload = self.discover([issue(140)])

        safety = payload["safety"]
        self.assertTrue(safety["read_only"])
        for field in (
            "ingested",
            "db_written",
            "workspace_prepared",
            "executor_started",
            "branch_pushed",
            "pr_created",
            "merged",
            "approved",
            "cleanup_performed",
        ):
            self.assertFalse(safety[field])

    def test_output_is_json_serializable(self) -> None:
        payload = self.discover([issue(150)])

        json.dumps(payload, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
