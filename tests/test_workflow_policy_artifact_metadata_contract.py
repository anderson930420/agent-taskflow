"""Tests for docs/workflow-policy-artifact-metadata-contract.md."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "workflow-policy-artifact-metadata-contract.md"


class WorkflowPolicyArtifactMetadataContractTests(unittest.TestCase):
    """Tests verifying the workflow policy artifact metadata contract doc."""

    @classmethod
    def setUpClass(cls) -> None:
        if not DOC_PATH.exists():
            raise FileNotFoundError(f"Contract doc not found: {DOC_PATH}")
        cls.content = DOC_PATH.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC_PATH.exists())

    def test_doc_mentions_workflow_policy_summary_json(self) -> None:
        self.assertIn("workflow_policy_summary.json", self.content)

    def test_doc_mentions_artifact_index_json(self) -> None:
        self.assertIn("artifact_index.json", self.content)

    def test_doc_mentions_workflow_policy_summary_artifact_type(self) -> None:
        self.assertIn("workflow_policy_summary", self.content)

    def test_doc_mentions_artifact_index_artifact_type(self) -> None:
        self.assertIn("artifact_index", self.content)

    def test_doc_mentions_workflow_policy_review_evidence_kind(self) -> None:
        self.assertIn("workflow_policy", self.content)

    def test_doc_mentions_workflow_policy_proof_of_work_package_type(self) -> None:
        self.assertIn("workflow_policy_proof_of_work", self.content)

    def test_doc_lists_artifact_index_top_level_fields(self) -> None:
        self.assertIn("artifact_index_version", self.content)
        self.assertIn("package_type", self.content)
        self.assertIn("generated_at", self.content)
        self.assertIn("artifacts", self.content)

    def test_doc_lists_artifact_entry_fields(self) -> None:
        self.assertIn("name", self.content)
        self.assertIn("artifact_type", self.content)
        self.assertIn("path", self.content)
        self.assertIn("required", self.content)
        self.assertIn("description", self.content)

    def test_doc_clarifies_artifact_index_name_and_path_semantics(self) -> None:
        self.assertIn("Logical artifact name", self.content)
        self.assertIn("top-level evidence object's `name`", self.content)
        self.assertIn("`path` is the artifact filename", self.content)

    def test_doc_lists_workflow_policy_summary_required_fields(self) -> None:
        for field in (
            "artifact_type",
            "schema_version",
            "source_path",
            "validation_status",
            "allowed_executors",
            "required_validators",
            "path_policy",
            "workspace_policy",
            "proof_of_work",
            "human_review",
            "forbidden_actions",
            "deferred_integrations",
            "governance_invariants",
            "generated_at",
        ):
            self.assertIn(field, self.content, f"required field {field!r} missing from contract doc")

    def test_doc_lists_required_artifact_entry_for_summary(self) -> None:
        # The doc should show the required artifact entry with name=workflow_policy_summary.
        self.assertIn("workflow_policy_summary", self.content)
        # Check for "required": true in the JSON code block (quoted key).
        self.assertIn('"required": true', self.content)

    def test_doc_states_other_remains_supported(self) -> None:
        self.assertIn("other", self.content)
        self.assertIn("backward", self.content.lower())

    def test_doc_states_no_dispatcher_enforcement(self) -> None:
        self.assertIn("dispatcher", self.content)
        self.assertIn("enforcement", self.content)

    def test_doc_states_no_runtime_enforcement(self) -> None:
        self.assertIn("runtime", self.content.lower())
        # Should state that artifact types do not imply enforcement.
        enforcement_mentions = self.content.lower().count("enforcement")
        self.assertGreater(enforcement_mentions, 0)

    def test_doc_states_no_api_endpoints_added(self) -> None:
        self.assertIn("API", self.content)
        self.assertIn("endpoint", self.content)
        self.assertIn("not add", self.content)

    def test_doc_states_no_frontend_behavior(self) -> None:
        self.assertIn("Mission Control", self.content)
        self.assertIn("frontend", self.content.lower())
        self.assertIn("not add", self.content)

    def test_doc_states_stability_rule(self) -> None:
        self.assertIn("Stability", self.content)
        self.assertIn("not rename", self.content.lower())

    def test_doc_states_no_github_integration(self) -> None:
        self.assertIn("GitHub", self.content)
        self.assertIn("not add", self.content.lower())

    def test_doc_states_no_merge_push_cleanup(self) -> None:
        self.assertIn("merge", self.content.lower())
        self.assertIn("push", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_doc_states_no_ai_self_governance(self) -> None:
        self.assertIn("AI", self.content)
        self.assertIn("self-governance", self.content.lower())

    def test_doc_lists_non_goals(self) -> None:
        self.assertIn("Non-Goals", self.content)
        # Should list several non-goals.
        self.assertIn("dispatcher", self.content)
        self.assertIn("executor", self.content.lower())

    def test_doc_lists_migration_compatibility(self) -> None:
        self.assertIn("Migration", self.content)
        self.assertIn("backward", self.content.lower())

    def test_doc_has_reference_implementation_section(self) -> None:
        self.assertIn("Reference Implementation", self.content)
        self.assertIn("scripts/write_workflow_policy_summary_artifact", self.content)
        self.assertIn("scripts/run_workflow_policy_pow_package_smoke", self.content)
        self.assertIn("scripts/run_workflow_policy_review_evidence_smoke", self.content)

    def test_doc_covers_artifact_index_version_field(self) -> None:
        self.assertIn("artifact_index_version", self.content)
        self.assertIn("0.1", self.content)

    def test_doc_covers_validation_status_field(self) -> None:
        self.assertIn("validation_status", self.content)
        self.assertIn("passed", self.content)
        self.assertIn("failed", self.content)

    def test_doc_describes_governance_invariants_as_object(self) -> None:
        self.assertIn("`governance_invariants` | object", self.content)
        self.assertIn('"ai_workers_may_approve": false', self.content)

    def test_doc_states_db_schema_unchanged(self) -> None:
        self.assertIn("schema", self.content.lower())
        self.assertIn("unchanged", self.content.lower())


if __name__ == "__main__":
    unittest.main()
