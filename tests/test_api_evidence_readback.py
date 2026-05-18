"""Tests for read-only dogfood evidence readback API."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class EvidenceReadbackApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.artifact_root = self.root / "artifacts"
        self.handoff_root = self.root / "handoff"
        self.repo_path.mkdir()
        self.artifact_root.mkdir()
        self.handoff_root.mkdir()

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-EVIDENCE-001"
        self.artifact_dir = self.artifact_root / self.task_key
        self.artifact_dir.mkdir()
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                status="waiting_approval",
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
            )
        )

        self.client_context = TestClient(create_app(self.db_path))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def write_artifact_record(
        self,
        artifact_type: str,
        relative_path: str,
        content: str,
        *,
        outside_task_artifact_dir: bool = False,
    ) -> Path:
        if outside_task_artifact_dir:
            path = self.handoff_root / self.task_key / relative_path
        else:
            path = self.artifact_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.store.record_task_artifact(self.task_key, artifact_type, path)
        return path

    def get_evidence_item(self) -> dict[str, object]:
        response = self.client.get(f"/api/tasks/{self.task_key}/evidence")
        self.assertEqual(response.status_code, 200)
        return response.json()["item"]

    def test_evidence_endpoint_includes_task_key_and_available_true(self) -> None:
        self.write_artifact_record("issue_spec", "issue_spec.md", "# Issue\n")

        payload = self.get_evidence_item()

        self.assertEqual(payload["task_key"], self.task_key)
        self.assertTrue(payload["available"])

    def test_artifacts_are_grouped_by_dogfood_category(self) -> None:
        self.write_artifact_record("issue_spec", "issue_spec.md", "# Issue\n")
        self.write_artifact_record(
            "pr_handoff",
            "pr_handoff.json",
            json.dumps({"artifact_type": "pr_handoff"}),
            outside_task_artifact_dir=True,
        )
        self.write_artifact_record(
            "branch_push",
            "branch_push.json",
            json.dumps({"artifact_type": "branch_push"}),
        )
        self.write_artifact_record(
            "draft_pr",
            "draft_pr.json",
            json.dumps({"artifact_type": "draft_pr"}),
            outside_task_artifact_dir=True,
        )
        (self.artifact_dir / "pytest.log").write_text("passed", encoding="utf-8")
        self.store.record_validation_result(
            self.task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="tests passed",
            log_path=self.artifact_dir / "pytest.log",
        )

        payload = self.get_evidence_item()
        categories = payload["categories"]

        self.assertEqual(categories["issue"][0]["artifact_type"], "issue_spec")
        self.assertEqual(categories["handoff"][0]["artifact_type"], "pr_handoff")
        self.assertEqual(categories["publication"][0]["artifact_type"], "branch_push")
        self.assertEqual(categories["draft_pr"][0]["artifact_type"], "draft_pr")
        self.assertTrue(categories["validation"])
        self.assertTrue(payload["summary"]["has_issue_spec"])
        self.assertTrue(payload["summary"]["has_pr_handoff"])
        self.assertTrue(payload["summary"]["has_branch_push"])
        self.assertTrue(payload["summary"]["has_draft_pr"])
        self.assertEqual(
            payload["summary"]["validation_statuses"],
            [{"validator": "pytest", "status": "passed", "summary": "tests passed"}],
        )

    def test_missing_optional_groups_degrade_gracefully(self) -> None:
        self.write_artifact_record("issue_spec", "issue_spec.md", "# Issue\n")

        payload = self.get_evidence_item()

        self.assertEqual(payload["categories"]["handoff"], [])
        self.assertEqual(payload["categories"]["publication"], [])
        self.assertEqual(payload["categories"]["draft_pr"], [])
        self.assertFalse(payload["summary"]["has_pr_handoff"])
        self.assertFalse(payload["summary"]["has_branch_push"])
        self.assertFalse(payload["summary"]["has_draft_pr"])
        self.assertFalse(payload["summary"]["has_preflight"])

    def test_malformed_json_artifact_does_not_crash_endpoint(self) -> None:
        self.write_artifact_record("draft_pr", "draft_pr.json", "{not json")

        payload = self.get_evidence_item()

        self.assertTrue(payload["available"])
        self.assertEqual(payload["categories"]["draft_pr"][0]["artifact_type"], "draft_pr")

    def test_safety_object_explicitly_disables_mutations(self) -> None:
        payload = self.get_evidence_item()
        safety = payload["safety"]

        self.assertTrue(safety["read_only"])
        self.assertFalse(safety["push_available_from_this_endpoint"])
        self.assertFalse(safety["pr_creation_available_from_this_endpoint"])
        self.assertFalse(safety["merge_available_from_this_endpoint"])
        self.assertFalse(safety["cleanup_available_from_this_endpoint"])
        self.assertFalse(safety["approval_available_from_this_endpoint"])

    def test_existing_artifact_endpoint_behavior_is_not_broken(self) -> None:
        self.write_artifact_record("issue_spec", "issue_spec.md", "# Issue\n")

        response = self.client.get(f"/api/tasks/{self.task_key}/artifacts")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["artifact_type"], "issue_spec")


if __name__ == "__main__":
    unittest.main()
