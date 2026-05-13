"""Tests for the review evidence API endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class ReviewEvidenceApiTests(unittest.TestCase):
    """Tests for /api/tasks/{task_key}/review-evidence and artifact preview."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.artifact_root = self.root / "artifacts"
        self.repo_path.mkdir()
        self.artifact_root.mkdir()

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._seed_data()

        self.client_context = TestClient(create_app(self.db_path))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def _make_task(
        self,
        task_key: str,
        *,
        project: str = "agent-taskflow",
        status: str = "queued",
    ) -> TaskRecord:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return TaskRecord(
            task_key=task_key,
            project=project,
            board=project,
            status=status,
            repo_path=self.repo_path,
            artifact_dir=artifact_dir,
        )

    def _seed_data(self) -> None:
        self.store.upsert_task(self._make_task("AT-0100", status="queued"))

    def _get_artifact_dir(self, task_key: str) -> Path:
        return self.artifact_root / task_key

    # ------------------------------------------------------------------
    # review-evidence endpoint tests
    # ------------------------------------------------------------------

    def test_review_evidence_returns_404_for_missing_task(self) -> None:
        response = self.client.get("/api/tasks/AT-9999/review-evidence")
        self.assertEqual(response.status_code, 404)

    def test_review_evidence_returns_missing_when_no_artifacts(self) -> None:
        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(payload["task_key"], "AT-0100")
        self.assertFalse(payload["mission_contract"]["exists"])
        self.assertEqual(payload["mission_contract"]["status"], "missing")
        self.assertEqual(payload["artifacts"], [])
        self.assertEqual(payload["validator_results"], [])

    def test_review_evidence_returns_missing_when_no_contract(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "some.log").write_text("worker log output", encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertFalse(payload["mission_contract"]["exists"])
        self.assertEqual(payload["mission_contract"]["status"], "missing")
        # But artifacts should be present
        self.assertEqual(len(payload["artifacts"]), 1)
        self.assertEqual(payload["artifacts"][0]["name"], "some.log")

    def test_review_evidence_returns_contract_summary(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        contract = {
            "schema_version": "1",
            "task_key": "AT-0100",
            "goal": "Test goal",
            "repo_path": str(self.repo_path),
            "worktree_path": str(self.root / "worktree"),
            "artifact_dir": str(artifact_dir),
            "executor": "pi",
            "required_validators": ["pytest", "policy"],
            "forbidden_actions": ["push", "merge", "cleanup"],
            "expected_artifacts": ["executor_log"],
            "human_approval_required": True,
            "governance_rules": ["agent-taskflow is the control plane."],
        }
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(contract, indent=2), encoding="utf-8"
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        mission_contract = payload["mission_contract"]
        self.assertTrue(mission_contract["exists"])
        self.assertEqual(mission_contract["status"], "present")
        self.assertEqual(mission_contract["task_key"], "AT-0100")
        self.assertEqual(mission_contract["executor"], "pi")
        self.assertEqual(mission_contract["required_validators"], ["pytest", "policy"])
        self.assertIn("push", mission_contract["forbidden_actions"])
        self.assertTrue(mission_contract["human_approval_required"])
        self.assertGreater(len(mission_contract["governance_rules"]), 0)

    def test_review_evidence_returns_invalid_when_contract_malformed(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text("not valid json {", encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertFalse(payload["mission_contract"]["exists"])
        self.assertEqual(payload["mission_contract"]["status"], "invalid")

    def test_review_evidence_returns_invalid_when_required_field_missing(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        incomplete = {"schema_version": "1", "task_key": "AT-0100"}
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(incomplete), encoding="utf-8"
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertFalse(payload["mission_contract"]["exists"])
        self.assertEqual(payload["mission_contract"]["status"], "invalid")

    def test_review_evidence_returns_validator_results(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        self.store.record_validation_result(
            "AT-0100",
            "pytest",
            status="passed",
            exit_code=0,
            summary="all tests passed",
            log_path=artifact_dir / "pytest.log",
            artifacts={"log": str(artifact_dir / "pytest.log")},
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(len(payload["validator_results"]), 1)
        self.assertEqual(payload["validator_results"][0]["validator"], "pytest")
        self.assertEqual(payload["validator_results"][0]["status"], "passed")

    def test_review_evidence_includes_policy_warnings_on_failure(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        self.store.record_validation_result(
            "AT-0100",
            "policy",
            status="failed",
            exit_code=1,
            summary="policy check failed",
            log_path=artifact_dir / "policy-validate.log",
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(payload["policy_status"], "failed")
        self.assertGreater(len(payload["policy_warnings"]), 0)

    def test_review_evidence_includes_artifacts_with_metadata(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "pytest.log").write_text("test output", encoding="utf-8")
        (artifact_dir / "pi-executor.log").write_text("executor output", encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        names = {a["name"] for a in payload["artifacts"]}
        self.assertIn("pytest.log", names)
        self.assertIn("pi-executor.log", names)

        pytest_artifact = next(a for a in payload["artifacts"] if a["name"] == "pytest.log")
        self.assertEqual(pytest_artifact["kind"], "validator_log")
        self.assertTrue(pytest_artifact["is_validator_log"])

        executor_artifact = next(a for a in payload["artifacts"] if a["name"] == "pi-executor.log")
        self.assertEqual(executor_artifact["kind"], "executor_log")
        self.assertTrue(executor_artifact["is_executor_log"])

    def test_review_evidence_task_without_artifact_dir_returns_422(self) -> None:
        task = TaskRecord(
            task_key="AT-0100",
            project="agent-taskflow",
            status="queued",
            repo_path=self.repo_path,
            artifact_dir=None,
        )
        self.store.upsert_task(task)

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 422)
        self.assertIn("no artifact directory", response.json()["detail"])

    # ------------------------------------------------------------------
    # artifact preview endpoint tests
    # ------------------------------------------------------------------

    def test_artifact_preview_returns_404_for_missing_task(self) -> None:
        response = self.client.get("/api/tasks/AT-9999/artifacts/some.log")
        self.assertEqual(response.status_code, 404)

    def test_artifact_preview_returns_422_for_missing_artifact_dir(self) -> None:
        task = TaskRecord(
            task_key="AT-0100",
            project="agent-taskflow",
            status="queued",
            repo_path=self.repo_path,
            artifact_dir=None,
        )
        self.store.upsert_task(task)

        response = self.client.get("/api/tasks/AT-0100/artifacts/some.log")
        self.assertEqual(response.status_code, 422)

    def test_artifact_preview_returns_content_for_existing_file(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "pytest.log").write_text("PASSED all tests\n", encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/artifacts/pytest.log")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name"], "pytest.log")
        self.assertEqual(payload["content"], "PASSED all tests\n")
        self.assertFalse(payload["truncated"])
        self.assertIsNone(payload["preview_reason"])

    def test_artifact_preview_returns_422_for_missing_file(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        artifact_dir.mkdir(parents=True, exist_ok=True)

        response = self.client.get("/api/tasks/AT-0100/artifacts/nonexistent.log")
        self.assertEqual(response.status_code, 422)

    def test_artifact_preview_rejects_path_traversal(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # FastAPI normalizes URLs before routing; '../../../etc/passwd' becomes '..'.
        # '..' matches /api/tasks/{task_key} and returns the task detail (200).
        # The actual path traversal IS blocked at the helper level (tested separately).
        # Test that a path with '..' is not accepted as a file name.
        with self.assertRaisesRegex(ValueError, "must not contain"):
            from agent_taskflow.api.review import build_artifact_preview
            build_artifact_preview(artifact_dir, "../../../etc/passwd")

    def test_artifact_preview_rejects_absolute_path(self) -> None:
        # Absolute-looking paths are rejected by the helper.
        artifact_dir = self._get_artifact_dir("AT-0100")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            from agent_taskflow.api.review import build_artifact_preview
            build_artifact_preview(artifact_dir, "/etc/passwd")

    def test_artifact_preview_returns_422_for_directory(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "subdir").mkdir()

        response = self.client.get("/api/tasks/AT-0100/artifacts/subdir")
        self.assertEqual(response.status_code, 422)

    def test_artifact_preview_redacts_secrets(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "env.log").write_text(
            "OPENAI_API_KEY=sk-abc123xyz\n", encoding="utf-8"
        )

        response = self.client.get("/api/tasks/AT-0100/artifacts/env.log")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["content"])
        self.assertIsNotNone(payload["preview_reason"])
        self.assertIn("secret", payload["preview_reason"].lower())

    def test_artifact_preview_skips_binary_files(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        response = self.client.get("/api/tasks/AT-0100/artifacts/image.png")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["content"])
        self.assertEqual(payload["preview_reason"], "binary file")

    def test_artifact_preview_truncates_large_files(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        # Write content larger than MAX_PREVIEW_SIZE (20 KB)
        large_content = "x" * (25 * 1024)
        (artifact_dir / "large.log").write_text(large_content, encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/artifacts/large.log")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["content"]), 20 * 1024)
        # Large content should not contain secret patterns
        self.assertFalse(payload["content"].startswith("secret"))

    # ------------------------------------------------------------------
    # review.py helper tests
    # ------------------------------------------------------------------

    def test_review_evidence_response_matches_expected_schema(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        contract = {
            "schema_version": "1",
            "task_key": "AT-0100",
            "goal": "Test",
            "repo_path": str(self.repo_path),
            "worktree_path": str(self.root / "wt"),
            "artifact_dir": str(artifact_dir),
            "executor": "pi",
            "required_validators": ["pytest"],
            "forbidden_actions": ["push"],
            "expected_artifacts": ["log"],
            "human_approval_required": True,
            "governance_rules": ["rule"],
        }
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(contract), encoding="utf-8"
        )
        (artifact_dir / "pytest.log").write_text("PASSED", encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]

        # Check top-level keys
        self.assertIn("task_key", payload)
        self.assertIn("mission_contract", payload)
        self.assertIn("artifacts", payload)
        self.assertIn("validator_results", payload)
        self.assertIn("policy_status", payload)
        self.assertIn("policy_warnings", payload)

        # Check artifact file summary keys
        artifact = payload["artifacts"][0]
        self.assertIn("name", artifact)
        self.assertIn("kind", artifact)
        self.assertIn("size_bytes", artifact)
        self.assertIn("preview_available", artifact)
        self.assertIn("has_secret_warning", artifact)

    def test_review_evidence_no_write_actions(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        contract = {
            "schema_version": "1",
            "task_key": "AT-0100",
            "goal": "Test",
            "repo_path": str(self.repo_path),
            "worktree_path": str(self.root / "wt"),
            "artifact_dir": str(artifact_dir),
            "executor": "pi",
            "required_validators": [],
            "forbidden_actions": [],
            "expected_artifacts": [],
            "human_approval_required": True,
        }
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(contract), encoding="utf-8"
        )

        before_files = set(artifact_dir.iterdir())
        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        after_files = set(artifact_dir.iterdir())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(before_files, after_files, "review-evidence should not modify files")


    # ------------------------------------------------------------------
    # artifact list endpoint tests
    # ------------------------------------------------------------------

    def test_artifact_list_returns_db_records_when_present(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "worker.log").write_text("executed", encoding="utf-8")
        self.store.record_task_artifact(
            "AT-0100",
            "worker_log",
            artifact_dir / "worker.log",
        )

        response = self.client.get("/api/tasks/AT-0100/artifacts")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreater(payload["count"], 0)

    def test_artifact_list_falls_back_to_filesystem_when_no_db_records(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text(
            '{"schema_version":"1","task_key":"AT-0100"}', encoding="utf-8"
        )
        (artifact_dir / "policy-validate.log").write_text("PASSED", encoding="utf-8")
        (artifact_dir / "pi-executor.log").write_text("done", encoding="utf-8")
        (artifact_dir / "handoff_summary.md").write_text("summary", encoding="utf-8")

        response = self.client.get("/api/tasks/AT-0100/artifacts")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 4)
        names = {item["name"] for item in payload["items"]}
        self.assertIn("mission_contract.json", names)
        self.assertIn("policy-validate.log", names)
        self.assertIn("pi-executor.log", names)
        self.assertIn("handoff_summary.md", names)

        # Verify kind classification.
        contract_item = next(i for i in payload["items"] if i["name"] == "mission_contract.json")
        self.assertEqual(contract_item["kind"], "mission_contract")
        self.assertTrue(contract_item["is_mission_contract"])

        policy_item = next(i for i in payload["items"] if i["name"] == "policy-validate.log")
        self.assertEqual(policy_item["kind"], "validator_log")
        self.assertTrue(policy_item["is_validator_log"])

        executor_item = next(i for i in payload["items"] if i["name"] == "pi-executor.log")
        self.assertEqual(executor_item["kind"], "executor_log")
        self.assertTrue(executor_item["is_executor_log"])

    def test_artifact_list_returns_empty_for_missing_artifact_dir(self) -> None:
        # Task with no artifact dir.
        task = TaskRecord(
            task_key="AT-0100",
            project="agent-taskflow",
            status="queued",
            repo_path=self.repo_path,
            artifact_dir=None,
        )
        self.store.upsert_task(task)

        response = self.client.get("/api/tasks/AT-0100/artifacts")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["items"], [])

    # ------------------------------------------------------------------
    # latest validator aggregation tests
    # ------------------------------------------------------------------

    def test_review_evidence_latest_validator_result_used(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "task_key": "AT-0100",
                    "goal": "Test",
                    "executor": "pi",
                    "repo_path": str(self.repo_path),
                    "worktree_path": str(self.root / "worktree"),
                    "artifact_dir": str(artifact_dir),
                    "required_validators": ["policy"],
                    "forbidden_actions": [],
                    "expected_artifacts": [],
                    "human_approval_required": True,
                    "governance_rules": [],
                },
            ),
            encoding="utf-8",
        )

        # Record first policy result as failed.
        self.store.record_validation_result(
            "AT-0100",
            "policy",
            status="failed",
            exit_code=1,
            summary="first run failed",
        )
        # Record second policy result as passed.
        self.store.record_validation_result(
            "AT-0100",
            "policy",
            status="passed",
            exit_code=0,
            summary="second run passed",
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]

        # Latest result should determine the aggregate.
        self.assertEqual(payload["policy_status"], "passed")
        self.assertEqual(payload["policy_warnings"], [])

        # Historical results should still be listed.
        self.assertEqual(len(payload["validator_results"]), 2)

    def test_review_evidence_old_passed_new_failed_uses_failed(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "task_key": "AT-0100",
                    "goal": "Test",
                    "executor": "pi",
                    "repo_path": str(self.repo_path),
                    "worktree_path": str(self.root / "worktree"),
                    "artifact_dir": str(artifact_dir),
                    "required_validators": ["policy"],
                    "forbidden_actions": [],
                    "expected_artifacts": [],
                    "human_approval_required": True,
                    "governance_rules": [],
                },
            ),
            encoding="utf-8",
        )

        self.store.record_validation_result(
            "AT-0100",
            "policy",
            status="passed",
            exit_code=0,
            summary="first run passed",
        )
        self.store.record_validation_result(
            "AT-0100",
            "policy",
            status="failed",
            exit_code=1,
            summary="second run failed",
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(payload["policy_status"], "failed")
        self.assertGreater(len(payload["policy_warnings"]), 0)

    def test_review_evidence_single_failed_result_returns_failed(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "task_key": "AT-0100",
                    "goal": "Test",
                    "executor": "pi",
                    "required_validators": ["policy"],
                    "forbidden_actions": [],
                    "expected_artifacts": [],
                    "human_approval_required": True,
                    "governance_rules": [],
                },
            ),
            encoding="utf-8",
        )

        self.store.record_validation_result(
            "AT-0100",
            "policy",
            status="failed",
            exit_code=1,
            summary="policy check failed",
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(payload["policy_status"], "failed")
        self.assertGreater(len(payload["policy_warnings"]), 0)

    def test_review_evidence_no_validator_results_shows_not_required(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "task_key": "AT-0100",
                    "goal": "Test",
                    "executor": "pi",
                    "required_validators": [],
                    "forbidden_actions": [],
                    "expected_artifacts": [],
                    "human_approval_required": True,
                    "governance_rules": [],
                },
            ),
            encoding="utf-8",
        )

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(payload["policy_status"], "not_required")
        self.assertEqual(payload["policy_warnings"], [])

    def test_review_evidence_policy_required_but_not_run_returns_not_run(self) -> None:
        artifact_dir = self._get_artifact_dir("AT-0100")
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "task_key": "AT-0100",
                    "goal": "Test",
                    "executor": "pi",
                    "repo_path": str(self.repo_path),
                    "worktree_path": str(self.root / "worktree"),
                    "artifact_dir": str(artifact_dir),
                    "required_validators": ["policy"],
                    "forbidden_actions": [],
                    "expected_artifacts": [],
                    "human_approval_required": True,
                    "governance_rules": [],
                },
            ),
            encoding="utf-8",
        )
        # No validation results recorded.

        response = self.client.get("/api/tasks/AT-0100/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual(payload["policy_status"], "not_run")
        self.assertGreater(len(payload["policy_warnings"]), 0)


class ReviewEvidenceHelpersTests(unittest.TestCase):
    """Unit tests for the review.py helper functions."""

    def test_contract_summary_missing(self) -> None:
        from agent_taskflow.api.review import build_contract_summary

        with tempfile.TemporaryDirectory() as tmp:
            result = build_contract_summary(Path(tmp))
            self.assertFalse(result["exists"])
            self.assertEqual(result["status"], "missing")

    def test_contract_summary_present(self) -> None:
        from agent_taskflow.api.review import build_contract_summary

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            contract = {
                "schema_version": "1",
                "task_key": "TEST",
                "goal": "Test goal",
                "repo_path": str(tmp),
                "worktree_path": str(tmp),
                "artifact_dir": str(tmp),
                "executor": "pi",
                "required_validators": ["pytest"],
                "forbidden_actions": ["push"],
                "expected_artifacts": [],
                "human_approval_required": True,
                "governance_rules": [],
            }
            (artifact_dir / "mission_contract.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
            result = build_contract_summary(artifact_dir)
            self.assertTrue(result["exists"])
            self.assertEqual(result["status"], "present")
            self.assertEqual(result["executor"], "pi")
            self.assertIn("pytest", result["required_validators"])

    def test_contract_summary_invalid_json(self) -> None:
        from agent_taskflow.api.review import build_contract_summary

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "mission_contract.json").write_text("not json", encoding="utf-8")
            result = build_contract_summary(artifact_dir)
            self.assertFalse(result["exists"])
            self.assertEqual(result["status"], "invalid")

    def test_artifact_file_summaries_lists_files(self) -> None:
        from agent_taskflow.api.review import build_artifact_file_summaries

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "pytest.log").write_text("passed", encoding="utf-8")
            (artifact_dir / "pi-executor.log").write_text("done", encoding="utf-8")

            results = build_artifact_file_summaries(artifact_dir)
            names = {r["name"] for r in results}
            self.assertIn("pytest.log", names)
            self.assertIn("pi-executor.log", names)

            pytest_result = next(r for r in results if r["name"] == "pytest.log")
            self.assertEqual(pytest_result["kind"], "validator_log")
            self.assertTrue(pytest_result["is_validator_log"])

    def test_artifact_file_summaries_skips_binary(self) -> None:
        from agent_taskflow.api.review import build_artifact_file_summaries

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "image.png").write_bytes(b"\x89PNG")
            results = build_artifact_file_summaries(artifact_dir)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["is_binary"])

    def test_artifact_preview_rejects_traversal(self) -> None:
        from agent_taskflow.api.review import build_artifact_preview

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "must not contain"):
                build_artifact_preview(Path(tmp), "../../../etc/passwd")

    def test_artifact_preview_rejects_absolute(self) -> None:
        from agent_taskflow.api.review import build_artifact_preview

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_artifact_preview(Path(tmp), "/etc/passwd")

    def test_artifact_preview_rejects_directory(self) -> None:
        from agent_taskflow.api.review import build_artifact_preview

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "subdir").mkdir()
            with self.assertRaisesRegex(ValueError, "not a file"):
                build_artifact_preview(artifact_dir, "subdir")

    def test_artifact_preview_content_returned(self) -> None:
        from agent_taskflow.api.review import build_artifact_preview

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "log.txt").write_text("hello world", encoding="utf-8")
            result = build_artifact_preview(artifact_dir, "log.txt")
            self.assertEqual(result["content"], "hello world")
            self.assertFalse(result["truncated"])
            self.assertIsNone(result["preview_reason"])

    def test_artifact_preview_secret_redacted(self) -> None:
        from agent_taskflow.api.review import build_artifact_preview

        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "env.log").write_text(
                "SECRET_KEY=topsecret123\n", encoding="utf-8"
            )
            result = build_artifact_preview(artifact_dir, "env.log")
            self.assertIsNone(result["content"])
            self.assertIsNotNone(result["preview_reason"])
            self.assertIn("secret", result["preview_reason"].lower())


if __name__ == "__main__":
    unittest.main()
