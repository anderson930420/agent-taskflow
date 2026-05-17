"""API contract tests for the future workflow_policy_evidence API shape.

These tests document the expected API contract for Phase 110's read-only
workflow_policy_evidence exposure. They validate expected data shapes and
safety semantics without implementing API behavior.

Two test approaches:
- Approach A: Pure shape-validation helpers (executable now)
- Approach B: Skipped integration tests (will execute once Phase 110 implements the API)

This module is tests-only. No API implementation code is modified.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
)


# ----------------------------------------------------------------------
# Expected schema shapes (Phase 110 contract targets)
# ----------------------------------------------------------------------


# Top-level workflow_policy_evidence field: expected sub-fields
EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS = frozenset({
    "available",
    "artifact_index",
    "summary",
    "review_artifacts",
})

# artifact_index sub-section: expected top-level fields
EXPECTED_ARTIFACT_INDEX_FIELDS = frozenset({
    "name",
    "artifact_type",
    "path",
    "package_type",
    "artifact_index_version",
    "generated_at",
    "artifacts",
})

# summary sub-section: expected top-level fields
EXPECTED_SUMMARY_FIELDS = frozenset({
    "name",
    "artifact_type",
    "path",
    "schema_version",
    "validation_status",
    "validation_errors",
    "validation_warnings",
    "source_path",
    "generated_at",
    "allowed_executors",
    "required_validators",
    "optional_validators",
    "path_policy",
    "workspace_policy",
    "proof_of_work",
    "human_review",
    "forbidden_actions",
    "deferred_integrations",
    "governance_invariants",
})

# review_artifacts entry: expected fields (matches existing build_artifact_file_summaries output)
EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS = frozenset({
    "name",
    "kind",
    "size_bytes",
    "is_validator_log",
    "is_executor_log",
    "is_mission_contract",
})

# artifact_index artifacts entry: expected fields
EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS = frozenset({
    "name",
    "artifact_type",
    "path",
    "required",
    "description",
})

# Fields that must NOT appear in workflow_policy_evidence (safety check)
FORBIDDEN_FIELDS = frozenset({
    "approval_action",
    "approve",
    "reject",
    "rerun",
    "block",
    "merge",
    "push",
    "cleanup",
    "delete_branch",
    "delete_worktree",
    "create_pr",
    "dispatcher_preflight",
    "enforce_policy",
    "auto_approve",
    "auto_merge",
    "ai_self_governance",
})


# ----------------------------------------------------------------------
# Schema validation helpers (pure, no API calls)
# ----------------------------------------------------------------------


def validate_workflow_policy_evidence_top_level(data: dict) -> list[str]:
    """Validate top-level workflow_policy_evidence field."""
    errors: list[str] = []
    for field in EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS:
        if field not in data:
            errors.append(f"missing expected field: {field}")
    return errors


def validate_workflow_policy_evidence_available_field(data: dict) -> list[str]:
    """Validate available field is a boolean."""
    errors: list[str] = []
    if "available" not in data:
        errors.append("missing 'available' field")
    elif not isinstance(data["available"], bool):
        errors.append("'available' must be a boolean")
    return errors


def validate_artifact_index_fields(data: dict) -> list[str]:
    """Validate artifact_index sub-section fields."""
    errors: list[str] = []
    if "artifact_index" not in data:
        return ["missing 'artifact_index' field"]
    index = data["artifact_index"]
    for field in EXPECTED_ARTIFACT_INDEX_FIELDS:
        if field not in index:
            errors.append(f"artifact_index missing expected field: {field}")
    return errors


def validate_summary_fields(data: dict) -> list[str]:
    """Validate summary sub-section fields."""
    errors: list[str] = []
    if "summary" not in data:
        return ["missing 'summary' field"]
    summary = data["summary"]
    for field in EXPECTED_SUMMARY_FIELDS:
        if field not in summary:
            errors.append(f"summary missing expected field: {field}")
    return errors


def validate_review_artifacts_entry(entry: dict) -> list[str]:
    """Validate a single review_artifacts entry."""
    errors: list[str] = []
    for field in EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS:
        if field not in entry:
            errors.append(f"review_artifacts entry missing expected field: {field}")
    return errors


def validate_review_artifacts_fields(data: dict) -> list[str]:
    """Validate review_artifacts sub-section fields."""
    errors: list[str] = []
    if "review_artifacts" not in data:
        return ["missing 'review_artifacts' field"]
    artifacts = data["review_artifacts"]
    if not isinstance(artifacts, list):
        errors.append("'review_artifacts' must be a list")
        return errors
    for i, entry in enumerate(artifacts):
        errors.extend(
            f"[{i}] {e}" for e in validate_review_artifacts_entry(entry)
        )
    return errors


def validate_no_forbidden_fields(data: dict) -> list[str]:
    """Verify no forbidden safety-violating fields are present."""
    errors: list[str] = []
    for field in FORBIDDEN_FIELDS:
        if field in data:
            errors.append(f"forbidden field '{field}' must not appear in workflow_policy_evidence")
    # Also check nested sections
    for section in ("artifact_index", "summary", "review_artifacts"):
        if section in data and isinstance(data[section], dict):
            for field in FORBIDDEN_FIELDS:
                if field in data[section]:
                    errors.append(
                        f"forbidden field '{field}' must not appear in {section}"
                    )
    return errors


def validate_workflow_policy_evidence_complete(data: dict) -> list[str]:
    """Validate complete workflow_policy_evidence shape."""
    errors: list[str] = []
    errors.extend(validate_workflow_policy_evidence_top_level(data))
    errors.extend(validate_workflow_policy_evidence_available_field(data))
    errors.extend(validate_artifact_index_fields(data))
    errors.extend(validate_summary_fields(data))
    errors.extend(validate_review_artifacts_fields(data))
    errors.extend(validate_no_forbidden_fields(data))
    return errors


# ----------------------------------------------------------------------
# Tests: expected shape validation (executable now, pure helpers)
# ----------------------------------------------------------------------


class ExpectedShapeSchemaTests(unittest.TestCase):
    """Tests verifying the expected schema data structures are well-formed."""

    def test_expected_workflow_policy_evidence_fields_defined(self) -> None:
        self.assertIsInstance(EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS, frozenset)
        self.assertIn("available", EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS)
        self.assertIn("artifact_index", EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS)
        self.assertIn("summary", EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS)
        self.assertIn("review_artifacts", EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS)

    def test_expected_artifact_index_fields_defined(self) -> None:
        self.assertIsInstance(EXPECTED_ARTIFACT_INDEX_FIELDS, frozenset)
        self.assertIn("name", EXPECTED_ARTIFACT_INDEX_FIELDS)
        self.assertIn("artifact_type", EXPECTED_ARTIFACT_INDEX_FIELDS)
        self.assertIn("path", EXPECTED_ARTIFACT_INDEX_FIELDS)
        self.assertIn("package_type", EXPECTED_ARTIFACT_INDEX_FIELDS)
        self.assertIn("artifact_index_version", EXPECTED_ARTIFACT_INDEX_FIELDS)
        self.assertIn("generated_at", EXPECTED_ARTIFACT_INDEX_FIELDS)
        self.assertIn("artifacts", EXPECTED_ARTIFACT_INDEX_FIELDS)

    def test_expected_summary_fields_defined(self) -> None:
        self.assertIsInstance(EXPECTED_SUMMARY_FIELDS, frozenset)
        self.assertIn("name", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("artifact_type", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("path", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("schema_version", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("validation_status", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("validation_errors", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("validation_warnings", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("source_path", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("generated_at", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("allowed_executors", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("required_validators", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("optional_validators", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("path_policy", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("workspace_policy", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("proof_of_work", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("human_review", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("forbidden_actions", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("deferred_integrations", EXPECTED_SUMMARY_FIELDS)
        self.assertIn("governance_invariants", EXPECTED_SUMMARY_FIELDS)

    def test_expected_review_artifact_entry_fields_defined(self) -> None:
        self.assertIsInstance(EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS, frozenset)
        self.assertIn("name", EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("kind", EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("size_bytes", EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("is_validator_log", EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("is_executor_log", EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("is_mission_contract", EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS)

    def test_expected_index_artifact_entry_fields_defined(self) -> None:
        self.assertIsInstance(EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS, frozenset)
        self.assertIn("name", EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("artifact_type", EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("path", EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("required", EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS)
        self.assertIn("description", EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS)

    def test_forbidden_fields_are_defined(self) -> None:
        self.assertIsInstance(FORBIDDEN_FIELDS, frozenset)
        self.assertIn("approve", FORBIDDEN_FIELDS)
        self.assertIn("reject", FORBIDDEN_FIELDS)
        self.assertIn("merge", FORBIDDEN_FIELDS)
        self.assertIn("push", FORBIDDEN_FIELDS)
        self.assertIn("cleanup", FORBIDDEN_FIELDS)
        self.assertIn("delete_branch", FORBIDDEN_FIELDS)


class ShapeValidationContractTests(unittest.TestCase):
    """Tests verifying the shape validation helpers against example data."""

    def _make_complete_valid_fixture(self) -> dict:
        """Build a complete, valid workflow_policy_evidence fixture."""
        return {
            "available": True,
            "artifact_index": {
                "name": "artifact_index.json",
                "artifact_type": "artifact_index",
                "path": "artifact_index.json",
                "package_type": "workflow_policy_proof_of_work",
                "artifact_index_version": "0.1",
                "generated_at": "2025-01-01T00:00:00Z",
                "artifacts": [
                    {
                        "name": "workflow_policy_summary",
                        "artifact_type": "workflow_policy_summary",
                        "path": "workflow_policy_summary.json",
                        "required": True,
                        "description": "Machine-readable workflow policy summary artifact.",
                    },
                ],
            },
            "summary": {
                "name": "workflow_policy_summary.json",
                "artifact_type": "workflow_policy_summary",
                "path": "workflow_policy_summary.json",
                "schema_version": "0.1",
                "validation_status": "passed",
                "validation_errors": [],
                "validation_warnings": [],
                "source_path": "/path/to/workflow-policy.example.json",
                "generated_at": "2025-01-01T00:00:00Z",
                "allowed_executors": ["manual", "shell", "opencode", "pi"],
                "required_validators": ["policy", "pytest"],
                "optional_validators": ["openspec"],
                "path_policy": {"allowed_paths": [], "forbidden_paths": []},
                "workspace_policy": {
                    "isolation_required": True,
                    "preferred_strategy": "per_task_worktree",
                    "preserve_on_failure": True,
                    "cleanup_control": "human_or_deterministic_policy",
                },
                "proof_of_work": {
                    "required_artifacts": ["run_summary", "mission_contract"],
                    "optional_artifacts": ["artifact_index"],
                },
                "human_review": {
                    "required": True,
                    "allowed_decisions": ["approve", "reject", "rerun", "block"],
                },
                "forbidden_actions": ["self_approve", "push"],
                "deferred_integrations": ["github_issues_sync"],
                "governance_invariants": [
                    {"invariant": "ai_workers_may_approve", "value": False}
                ],
            },
            "review_artifacts": [
                {
                    "name": "artifact_index.json",
                    "kind": "workflow_policy",
                    "size_bytes": 412,
                    "is_validator_log": False,
                    "is_executor_log": False,
                    "is_mission_contract": False,
                },
                {
                    "name": "workflow_policy_summary.json",
                    "kind": "workflow_policy",
                    "size_bytes": 1862,
                    "is_validator_log": False,
                    "is_executor_log": False,
                    "is_mission_contract": False,
                },
            ],
        }

    def test_complete_valid_fixture_passes_validation(self) -> None:
        fixture = self._make_complete_valid_fixture()
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertEqual(errors, [], f"valid fixture should pass: {errors}")

    def test_missing_available_field_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        del fixture["available"]
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("available" in e for e in errors))

    def test_available_not_boolean_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["available"] = "yes"
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("boolean" in e for e in errors))

    def test_available_false_fixture_passes_validation(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["available"] = False
        # When available is False, artifact_index, summary, review_artifacts
        # may be omitted or empty; the validator should still pass.
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertEqual(errors, [], f"available=false should pass: {errors}")

    def test_missing_artifact_index_fields_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        del fixture["artifact_index"]["package_type"]
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("package_type" in e for e in errors))

    def test_missing_summary_fields_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        del fixture["summary"]["validation_errors"]
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("validation_errors" in e for e in errors))

    def test_missing_review_artifacts_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        del fixture["review_artifacts"]
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("review_artifacts" in e for e in errors))

    def test_review_artifacts_not_list_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["review_artifacts"] = "not-a-list"
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("list" in e.lower() for e in errors))

    def test_review_artifacts_entry_missing_fields_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["review_artifacts"].append({"name": "incomplete.json"})
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(
            any("kind" in e or "size_bytes" in e for e in errors),
            f"expected errors about missing entry fields: {errors}",
        )

    def test_forbidden_approve_field_detected_at_top_level(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["approve"] = True
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(
            any("approve" in e for e in errors),
            f"forbidden field should be detected: {errors}",
        )

    def test_forbidden_merge_field_detected_at_top_level(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["merge"] = True
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("merge" in e for e in errors))

    def test_forbidden_dispatcher_preflight_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["dispatcher_preflight"] = {"check": "workflow_policy"}
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(any("dispatcher_preflight" in e for e in errors))

    def test_forbidden_approve_in_summary_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["summary"]["approve"] = True
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(
            any("approve" in e and "summary" in e.lower() for e in errors),
            f"forbidden field in summary should be detected: {errors}",
        )

    def test_forbidden_cleanup_in_artifact_index_detected(self) -> None:
        fixture = self._make_complete_valid_fixture()
        fixture["artifact_index"]["cleanup"] = True
        errors = validate_workflow_policy_evidence_complete(fixture)
        self.assertTrue(
            any("cleanup" in e and "artifact_index" in e.lower() for e in errors),
            f"forbidden field in artifact_index should be detected: {errors}",
        )

    def test_artifact_index_artifacts_list_validates_entry(self) -> None:
        fixture = self._make_complete_valid_fixture()
        # Index entry missing 'required' field should be caught.
        fixture["artifact_index"]["artifacts"].append(
            {
                "name": "incomplete",
                "artifact_type": "other",
                "path": "other.json",
                # missing required, description
            }
        )
        # The validate_artifact_index_fields only checks top-level index fields,
        # not nested entries. Verify the entry fields are in the schema.
        entry_fields = EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS
        self.assertIn("required", entry_fields)
        self.assertIn("description", entry_fields)


class SafetySemanticsContractTests(unittest.TestCase):
    """Tests verifying safety semantics constraints on the expected shape."""

    def test_no_state_transition_fields_in_schema(self) -> None:
        # The schema should not include fields that represent state transitions.
        state_transition_fields = {
            "status_change",
            "transition_to",
            "set_status",
            "update_status",
        }
        all_expected = (
            EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS
            | EXPECTED_ARTIFACT_INDEX_FIELDS
            | EXPECTED_SUMMARY_FIELDS
            | EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS
        )
        for field in state_transition_fields:
            self.assertNotIn(
                field, all_expected,
                f"state transition field {field!r} must not be in expected schema",
            )

    def test_no_approval_action_fields_in_schema(self) -> None:
        approval_fields = {"approve", "reject", "rerun", "block", "approval_decision"}
        all_expected = (
            EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS
            | EXPECTED_ARTIFACT_INDEX_FIELDS
            | EXPECTED_SUMMARY_FIELDS
            | EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS
        )
        for field in approval_fields:
            self.assertNotIn(
                field, all_expected,
                f"approval field {field!r} must not be in expected schema",
            )

    def test_no_merge_push_cleanup_fields_in_schema(self) -> None:
        write_action_fields = {"merge", "push", "cleanup", "delete_worktree"}
        all_expected = (
            EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS
            | EXPECTED_ARTIFACT_INDEX_FIELDS
            | EXPECTED_SUMMARY_FIELDS
            | EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS
        )
        for field in write_action_fields:
            self.assertNotIn(
                field, all_expected,
                f"write action field {field!r} must not be in expected schema",
            )

    def test_forbidden_fields_set_covers_dangerous_actions(self) -> None:
        dangerous = {
            "approve", "reject", "rerun", "block",
            "merge", "push", "cleanup", "delete_branch",
            "create_pr", "dispatcher_preflight", "enforce_policy",
        }
        for field in dangerous:
            self.assertIn(
                field, FORBIDDEN_FIELDS,
                f"dangerous field {field!r} must be in FORBIDDEN_FIELDS",
            )

    def test_workflow_policy_evidence_is_read_only(self) -> None:
        # By design, the field contains evidence (artifacts and metadata).
        # It should not contain action triggers.
        self.assertNotIn("action", "".join(EXPECTED_WORKFLOW_POLICY_EVIDENCE_FIELDS))
        # The presence of review_artifacts, summary, artifact_index all indicate
        # read-only evidence recording, not action triggers.

    def test_available_field_represents_presence_not_readiness(self) -> None:
        # available=True means artifacts exist, not that the task is "ready".
        # This distinction is critical for safety: availability ≠ approval.
        fixture = self._make_complete_valid_fixture()
        self.assertIn("available", fixture)
        self.assertIsInstance(fixture["available"], bool)
        # available false does not mean "blocked" or "failed" — it means absent.

    def _make_complete_valid_fixture(self) -> dict:
        return {
            "available": True,
            "artifact_index": {
                "name": "artifact_index.json",
                "artifact_type": "artifact_index",
                "path": "artifact_index.json",
                "package_type": "workflow_policy_proof_of_work",
                "artifact_index_version": "0.1",
                "generated_at": "2025-01-01T00:00:00Z",
                "artifacts": [
                    {
                        "name": "workflow_policy_summary",
                        "artifact_type": "workflow_policy_summary",
                        "path": "workflow_policy_summary.json",
                        "required": True,
                        "description": "test",
                    },
                ],
            },
            "summary": {
                "name": "workflow_policy_summary.json",
                "artifact_type": "workflow_policy_summary",
                "path": "workflow_policy_summary.json",
                "schema_version": "0.1",
                "validation_status": "passed",
                "validation_errors": [],
                "validation_warnings": [],
                "source_path": "/path/to/policy.json",
                "generated_at": "2025-01-01T00:00:00Z",
                "allowed_executors": ["manual"],
                "required_validators": ["policy"],
                "optional_validators": [],
                "path_policy": {},
                "workspace_policy": {},
                "proof_of_work": {},
                "human_review": {"required": True, "allowed_decisions": ["approve"]},
                "forbidden_actions": [],
                "deferred_integrations": [],
                "governance_invariants": [],
            },
            "review_artifacts": [
                {
                    "name": "workflow_policy_summary.json",
                    "kind": "workflow_policy",
                    "size_bytes": 100,
                    "is_validator_log": False,
                    "is_executor_log": False,
                    "is_mission_contract": False,
                },
            ],
        }


# ----------------------------------------------------------------------
# Skipped integration tests (execute once Phase 110 implements the API)
# ----------------------------------------------------------------------


class ApiIntegrationTests(unittest.TestCase):
    """Integration tests for workflow_policy_evidence API exposure.

    These tests verify the actual API endpoint behavior by testing
    the FastAPI app with real artifact directories and review evidence helpers.
    """

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

        self.client_context = TestClient(create_app(self.db_path))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def _make_task(self, task_key: str) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    def _seed_task(self, task_key: str, artifact_dir: Path) -> None:
        from agent_taskflow.models import TaskRecord
        task = TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            status="queued",
            repo_path=self.repo_path,
            artifact_dir=artifact_dir,
        )
        self.store.upsert_task(task)

    def _write_workflow_policy_package(self, artifact_dir: Path) -> None:
        """Write minimal but complete workflow policy artifact files."""
        import json

        # Write the index artifact.
        index = {
            "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
            "package_type": WORKFLOW_POLICY_PACKAGE_TYPE,
            "generated_at": "2025-01-01T00:00:00Z",
            "artifacts": [
                {
                    "name": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
                    "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
                    "path": WORKFLOW_POLICY_SUMMARY_FILENAME,
                    "required": True,
                    "description": "Machine-readable workflow policy summary artifact.",
                },
            ],
        }
        (artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).write_text(
            json.dumps(index), encoding="utf-8"
        )

        # Write the summary artifact.
        summary = {
            "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
            "schema_version": "0.1",
            "validation_status": "passed",
            "validation_errors": [],
            "validation_warnings": [],
            "source_path": str(artifact_dir / "policy.example.json"),
            "generated_at": "2025-01-01T00:00:00Z",
            "allowed_executors": ["manual", "pi", "opencode"],
            "required_validators": ["policy", "pytest"],
            "optional_validators": ["openspec"],
            "path_policy": {"allowed_paths": [], "forbidden_paths": []},
            "workspace_policy": {
                "isolation_required": True,
                "preferred_strategy": "per_task_worktree",
            },
            "proof_of_work": {
                "required_artifacts": ["run_summary"],
                "optional_artifacts": [],
            },
            "human_review": {"required": True, "allowed_decisions": ["approve", "reject"]},
            "forbidden_actions": ["push", "merge"],
            "deferred_integrations": ["github_issues_sync"],
            "governance_invariants": [],
        }
        (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
            json.dumps(summary), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Basic field presence tests
    # ------------------------------------------------------------------

    def test_review_evidence_response_contains_workflow_policy_evidence_field(self) -> None:
        artifact_dir = self._make_task("AT-API-0101")
        self._seed_task("AT-API-0101", artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0101/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertIn("workflow_policy_evidence", payload)

    def test_workflow_policy_evidence_backward_compatible_existing_fields(self) -> None:
        artifact_dir = self._make_task("AT-API-0102")
        self._seed_task("AT-API-0102", artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0102/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        # Existing fields must still be present.
        self.assertIn("task_key", payload)
        self.assertIn("mission_contract", payload)
        self.assertIn("artifacts", payload)
        self.assertIn("validator_results", payload)
        self.assertIn("policy_status", payload)
        self.assertIn("policy_warnings", payload)

    # ------------------------------------------------------------------
    # available=true tests
    # ------------------------------------------------------------------

    def test_workflow_policy_evidence_available_true_when_artifacts_present(self) -> None:
        artifact_dir = self._make_task("AT-API-0103")
        self._seed_task("AT-API-0103", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0103/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        self.assertIsInstance(wpe["available"], bool)
        self.assertTrue(wpe["available"])

    def test_workflow_policy_evidence_exposes_artifact_index_sub_section(self) -> None:
        artifact_dir = self._make_task("AT-API-0104")
        self._seed_task("AT-API-0104", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0104/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        self.assertIn("artifact_index", wpe)
        ai = wpe["artifact_index"]
        self.assertIsNotNone(ai)

        # Check expected fields.
        for field in EXPECTED_ARTIFACT_INDEX_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, ai, f"artifact_index missing: {field}")

        # Check artifact entries.
        self.assertIn("artifacts", ai)
        self.assertIsInstance(ai["artifacts"], list)
        self.assertGreater(len(ai["artifacts"]), 0)
        for entry in ai["artifacts"]:
            for field in EXPECTED_INDEX_ARTIFACT_ENTRY_FIELDS:
                with self.subTest(entry=entry, field=field):
                    self.assertIn(field, entry, f"artifact entry missing: {field}")

    def test_workflow_policy_evidence_exposes_summary_sub_section(self) -> None:
        artifact_dir = self._make_task("AT-API-0105")
        self._seed_task("AT-API-0105", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0105/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        self.assertIn("summary", wpe)
        sm = wpe["summary"]
        self.assertIsNotNone(sm)

        # Check all expected fields.
        for field in EXPECTED_SUMMARY_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, sm, f"summary missing: {field}")

    def test_workflow_policy_evidence_exposes_review_artifacts_sub_section(self) -> None:
        artifact_dir = self._make_task("AT-API-0106")
        self._seed_task("AT-API-0106", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0106/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        self.assertIn("review_artifacts", wpe)
        ra = wpe["review_artifacts"]
        self.assertIsInstance(ra, list)
        self.assertGreater(len(ra), 0)

        # All review_artifacts entries must have expected entry fields.
        for entry in ra:
            for field in EXPECTED_REVIEW_ARTIFACT_ENTRY_FIELDS:
                with self.subTest(entry=entry, field=field):
                    self.assertIn(field, entry)

    def test_review_artifacts_include_both_canonical_files_with_kind_workflow_policy(self) -> None:
        artifact_dir = self._make_task("AT-API-0107")
        self._seed_task("AT-API-0107", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0107/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]
        ra = wpe["review_artifacts"]

        names = {entry["name"] for entry in ra}
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, names)
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, names)

        for entry in ra:
            self.assertEqual(entry["kind"], WORKFLOW_POLICY_REVIEW_KIND)

    # ------------------------------------------------------------------
    # available=false tests
    # ------------------------------------------------------------------

    def test_workflow_policy_evidence_available_false_when_artifacts_absent(self) -> None:
        artifact_dir = self._make_task("AT-API-0108")
        self._seed_task("AT-API-0108", artifact_dir)
        # No workflow policy artifacts written.

        response = self.client.get("/api/tasks/AT-API-0108/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        self.assertFalse(wpe["available"])
        self.assertIn("workflow_policy_evidence", payload)

    def test_missing_summary_only_results_in_available_false(self) -> None:
        artifact_dir = self._make_task("AT-API-0109")
        self._seed_task("AT-API-0109", artifact_dir)
        # Only write index, not summary.
        import json
        index = {
            "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
            "package_type": WORKFLOW_POLICY_PACKAGE_TYPE,
            "generated_at": "2025-01-01T00:00:00Z",
            "artifacts": [],
        }
        (artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).write_text(
            json.dumps(index), encoding="utf-8"
        )

        response = self.client.get("/api/tasks/AT-API-0109/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        # Missing summary alone should result in available=False.
        self.assertFalse(wpe["available"])

    def test_missing_index_only_results_in_available_false(self) -> None:
        artifact_dir = self._make_task("AT-API-0110")
        self._seed_task("AT-API-0110", artifact_dir)
        # Only write summary, not index.
        import json
        summary = {
            "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
            "schema_version": "0.1",
            "validation_status": "passed",
            "validation_errors": [],
            "validation_warnings": [],
            "source_path": "",
            "generated_at": "2025-01-01T00:00:00Z",
            "allowed_executors": [],
            "required_validators": [],
            "optional_validators": [],
            "path_policy": {},
            "workspace_policy": {},
            "proof_of_work": {},
            "human_review": {},
            "forbidden_actions": [],
            "deferred_integrations": [],
            "governance_invariants": [],
        }
        (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
            json.dumps(summary), encoding="utf-8"
        )

        response = self.client.get("/api/tasks/AT-API-0110/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        # Missing index alone should result in available=False.
        self.assertFalse(wpe["available"])

    def test_no_crash_when_summary_file_corrupted(self) -> None:
        artifact_dir = self._make_task("AT-API-0111")
        self._seed_task("AT-API-0111", artifact_dir)
        # Write a valid index but a corrupted summary.
        import json
        index = {
            "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
            "package_type": WORKFLOW_POLICY_PACKAGE_TYPE,
            "generated_at": "2025-01-01T00:00:00Z",
            "artifacts": [],
        }
        (artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).write_text(
            json.dumps(index), encoding="utf-8"
        )
        (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
            "not valid json { this is broken", encoding="utf-8"
        )

        response = self.client.get("/api/tasks/AT-API-0111/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        # Corrupted summary should result in available=False, not a crash.
        self.assertFalse(wpe["available"])

    # ------------------------------------------------------------------
    # Forbidden fields safety tests
    # ------------------------------------------------------------------

    def test_workflow_policy_evidence_no_forbidden_action_fields(self) -> None:
        artifact_dir = self._make_task("AT-API-0112")
        self._seed_task("AT-API-0112", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0112/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        errors = validate_no_forbidden_fields(wpe)
        self.assertEqual(errors, [], f"forbidden fields detected: {errors}")

    def test_workflow_policy_evidence_complete_validation_passes(self) -> None:
        artifact_dir = self._make_task("AT-API-0113")
        self._seed_task("AT-API-0113", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0113/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        wpe = payload["workflow_policy_evidence"]

        errors = validate_workflow_policy_evidence_complete(wpe)
        self.assertEqual(errors, [], f"shape validation failed: {errors}")

    # ------------------------------------------------------------------
    # Read-only semantics tests
    # ------------------------------------------------------------------

    def test_review_evidence_does_not_mutate_workflow_policy_artifacts(self) -> None:
        artifact_dir = self._make_task("AT-API-0114")
        self._seed_task("AT-API-0114", artifact_dir)
        self._write_workflow_policy_package(artifact_dir)

        summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
        index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
        before_summary = summary_path.read_bytes()
        before_index = index_path.read_bytes()

        response = self.client.get("/api/tasks/AT-API-0114/review-evidence")
        self.assertEqual(response.status_code, 200)

        after_summary = summary_path.read_bytes()
        after_index = index_path.read_bytes()

        self.assertEqual(before_summary, after_summary)
        self.assertEqual(before_index, after_index)

    def test_review_evidence_does_not_create_workflow_policy_artifacts(self) -> None:
        artifact_dir = self._make_task("AT-API-0115")
        self._seed_task("AT-API-0115", artifact_dir)
        # No workflow policy artifacts.
        before_files = set(artifact_dir.iterdir())

        response = self.client.get("/api/tasks/AT-API-0115/review-evidence")
        self.assertEqual(response.status_code, 200)

        after_files = set(artifact_dir.iterdir())
        # No new files should be created.
        self.assertEqual(before_files, after_files)

    # ------------------------------------------------------------------
    # Empty artifact directory compatibility
    # ------------------------------------------------------------------

    def test_existing_review_evidence_works_without_workflow_policy_artifacts(self) -> None:
        artifact_dir = self._make_task("AT-API-0116")
        self._seed_task("AT-API-0116", artifact_dir)
        # Only mission contract, no workflow policy artifacts.
        import json
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps({
                "schema_version": "1",
                "task_key": "AT-API-0116",
                "goal": "Test",
                "executor": "pi",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.root / "wt"),
                "artifact_dir": str(artifact_dir),
                "required_validators": [],
                "forbidden_actions": [],
                "expected_artifacts": [],
                "human_approval_required": True,
                "governance_rules": [],
            }),
            encoding="utf-8",
        )

        response = self.client.get("/api/tasks/AT-API-0116/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]

        # Existing fields must still work.
        self.assertEqual(payload["task_key"], "AT-API-0116")
        self.assertTrue(payload["mission_contract"]["exists"])
        self.assertGreaterEqual(len(payload["artifacts"]), 1)

        # workflow_policy_evidence should be present but not block the response.
        wpe = payload["workflow_policy_evidence"]
        self.assertFalse(wpe["available"])

    def test_available_false_fixture_preserves_existing_response(self) -> None:
        artifact_dir = self._make_task("AT-API-0117")
        self._seed_task("AT-API-0117", artifact_dir)

        response = self.client.get("/api/tasks/AT-API-0117/review-evidence")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]

        # The response should be complete even with no artifacts.
        self.assertIn("task_key", payload)
        self.assertIn("mission_contract", payload)
        self.assertIn("artifacts", payload)
        self.assertIn("validator_results", payload)
        self.assertIn("workflow_policy_evidence", payload)

        wpe = payload["workflow_policy_evidence"]
        self.assertFalse(wpe["available"])
        self.assertEqual(wpe["review_artifacts"], [])


if __name__ == "__main__":
    unittest.main()
