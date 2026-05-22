from __future__ import annotations

import json
import importlib
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_recommendations import (
    TaskRecommendationRequest,
    recommend_next_tasks,
)


class TaskRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifacts = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_task(
        self,
        task_key: str,
        *,
        title: str,
        project: str = "agent-taskflow",
        created_at: str = "2026-05-01T00:00:00Z",
        updated_at: str = "2026-05-01T00:00:00Z",
        labels: tuple[str, ...] = (),
        issue_spec: bool = True,
    ) -> None:
        artifact_dir = self.artifacts / task_key
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=project,
                board=project,
                title=title,
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        self.store.record_task_event(
            task_key,
            "github_issue_ingested",
            "github",
            payload={
                "kind": "github_issue_ingested",
                "repo": "anderson930420/agent-taskflow",
                "issue_number": int(task_key.rsplit("-", 1)[-1]),
                "labels": list(labels),
                "selected_intake": True,
            },
        )
        if issue_spec:
            self.store.record_task_artifact(task_key, "issue_spec", artifact_dir / "issue_spec.md")

    def recommend(self, **overrides: object) -> dict[str, object]:
        request = TaskRecommendationRequest(db_path=self.db_path, **overrides)
        return recommend_next_tasks(request)

    def test_no_queued_tasks_returns_empty_recommendation(self) -> None:
        payload = self.recommend()

        self.assertEqual(payload["recommended_next_task"], None)
        self.assertEqual(payload["ranked_tasks"], [])
        self.assertEqual(payload["summary"]["queued_task_count"], 0)
        self.assertEqual(payload["summary"]["recommended_count"], 0)
        self.assertEqual(payload["summary"]["blocked_or_excluded_count"], 0)

    def test_one_queued_task_is_recommended(self) -> None:
        self.make_task("AT-GH-201", title="Single queued task", labels=("ready",))

        payload = self.recommend()

        self.assertEqual(payload["recommended_next_task"]["task_key"], "AT-GH-201")
        self.assertTrue(payload["recommended_next_task"]["requires_human_confirmation"])
        self.assertEqual(payload["ranked_tasks"][0]["task_key"], "AT-GH-201")

    def test_multiple_queued_tasks_rank_deterministically(self) -> None:
        self.make_task("AT-GH-202", title="Ready task", labels=("ready",))
        self.make_task("AT-GH-203", title="Older plain task", created_at="2026-04-01T00:00:00Z")
        self.make_task("AT-GH-204", title="Newer plain task", created_at="2026-05-02T00:00:00Z")

        payload = self.recommend()

        self.assertEqual(
            [item["task_key"] for item in payload["ranked_tasks"]],
            ["AT-GH-202", "AT-GH-203", "AT-GH-204"],
        )

    def test_ready_label_ranks_above_plain_queued_task(self) -> None:
        self.make_task("AT-GH-205", title="Plain queued task")
        self.make_task("AT-GH-206", title="Ready queued task", labels=("ready",))

        payload = self.recommend()

        self.assertEqual(payload["ranked_tasks"][0]["task_key"], "AT-GH-206")
        self.assertIn("ready label", payload["ranked_tasks"][0]["reason"])

    def test_older_queued_task_ranks_above_newer_when_signals_match(self) -> None:
        self.make_task("AT-GH-207", title="Older task", created_at="2026-04-01T00:00:00Z")
        self.make_task("AT-GH-208", title="Newer task", created_at="2026-05-01T00:00:00Z")

        payload = self.recommend()

        self.assertEqual(
            [item["task_key"] for item in payload["ranked_tasks"]],
            ["AT-GH-207", "AT-GH-208"],
        )

    def test_blocked_label_task_is_excluded(self) -> None:
        self.make_task("AT-GH-209", title="Blocked task", labels=("blocked",))

        payload = self.recommend()

        self.assertEqual(payload["ranked_tasks"], [])
        self.assertEqual(payload["blocked_or_excluded"][0]["task_key"], "AT-GH-209")
        self.assertIn("blocked label", payload["blocked_or_excluded"][0]["reason"])

    def test_high_risk_task_is_ranked_lower_and_marked_high_risk(self) -> None:
        self.make_task("AT-GH-210", title="Plain task")
        self.make_task("AT-GH-211", title="High risk task", labels=("high-risk",))

        payload = self.recommend()

        self.assertEqual(payload["ranked_tasks"][-1]["task_key"], "AT-GH-211")
        self.assertEqual(payload["ranked_tasks"][-1]["risk_level"], "high")

    def test_summary_counts_are_correct(self) -> None:
        self.make_task("AT-GH-212", title="Ready task", labels=("ready",))
        self.make_task("AT-GH-213", title="Blocked task", labels=("blocked",))
        self.make_task("AT-GH-214", title="High risk task", labels=("high-risk",))

        payload = self.recommend()

        self.assertEqual(
            payload["summary"],
            {
                "queued_task_count": 3,
                "recommended_count": 2,
                "blocked_or_excluded_count": 1,
            },
        )

    def test_safety_block_is_explicit_and_read_only(self) -> None:
        self.make_task("AT-GH-215", title="Ready task", labels=("ready",))

        payload = self.recommend()
        safety = payload["safety"]

        self.assertTrue(safety["read_only"])
        for field in (
            "task_status_changed",
            "db_written",
            "artifact_written",
            "workspace_prepared",
            "executor_started",
            "validators_started",
            "branch_pushed",
            "pr_created",
            "merged",
            "approved",
            "cleanup_performed",
        ):
            self.assertFalse(safety[field])

    def test_missing_db_path_does_not_create_a_db_file(self) -> None:
        missing = self.root / "missing" / "state.db"
        payload = recommend_next_tasks(
            TaskRecommendationRequest(db_path=missing, limit=5),
        )

        self.assertFalse(missing.exists())
        self.assertEqual(payload["summary"]["queued_task_count"], 0)
        self.assertEqual(payload["ranked_tasks"], [])
        self.assertEqual(payload["recommended_next_task"], None)
        self.assertTrue(payload["safety"]["read_only"])

    def test_output_is_json_serializable(self) -> None:
        self.make_task("AT-GH-216", title="Ready task", labels=("ready",))

        payload = self.recommend()
        json.dumps(payload, sort_keys=True)

    def test_legacy_singular_module_reexports_canonical_api(self) -> None:
        legacy = importlib.import_module("agent_taskflow.task_recommendation")
        canonical = importlib.import_module("agent_taskflow.task_recommendations")

        self.assertIs(
            legacy.TaskRecommendationRequest, canonical.TaskRecommendationRequest
        )
        self.assertIs(legacy.TaskRecommendationError, canonical.TaskRecommendationError)
        self.assertIs(legacy.recommend_next_tasks, canonical.recommend_next_tasks)


if __name__ == "__main__":
    unittest.main()
