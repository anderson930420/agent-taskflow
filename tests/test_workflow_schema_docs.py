"""Tests for the draft workflow policy schema documentation and example."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DOC = REPO_ROOT / "docs" / "workflow-schema.md"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"


class WorkflowSchemaDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = SCHEMA_DOC.read_text(encoding="utf-8")
        cls.policy = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))

    def test_schema_doc_exists(self) -> None:
        self.assertTrue(SCHEMA_DOC.is_file())

    def test_example_policy_exists(self) -> None:
        self.assertTrue(EXAMPLE_POLICY.is_file())

    def test_example_includes_schema_version(self) -> None:
        self.assertEqual(self.policy["schema_version"], "0.1")

    def test_example_includes_allowed_executors(self) -> None:
        self.assertEqual(
            self.policy["allowed_executors"],
            ["manual", "shell", "opencode", "pi"],
        )

    def test_example_includes_required_validators(self) -> None:
        for validator in ["policy", "changed-files", "pytest", "typecheck", "lint"]:
            self.assertIn(validator, self.policy["required_validators"])

    def test_example_includes_path_policy(self) -> None:
        self.assertIn("allowed_paths", self.policy["path_policy"])
        self.assertIn("forbidden_paths", self.policy["path_policy"])

    def test_example_includes_proof_of_work(self) -> None:
        proof_of_work = self.policy["proof_of_work"]
        self.assertIn("required_artifacts", proof_of_work)
        self.assertIn("optional_artifacts", proof_of_work)
        self.assertIn("mission_contract", proof_of_work["required_artifacts"])
        self.assertIn("changed_files_audit", proof_of_work["required_artifacts"])

    def test_example_includes_human_review(self) -> None:
        human_review = self.policy["human_review"]
        self.assertTrue(human_review["required"])
        self.assertEqual(
            human_review["allowed_decisions"],
            ["approve", "reject", "rerun", "block"],
        )

    def test_example_includes_forbidden_actions(self) -> None:
        for action in [
            "self_approve",
            "approve_without_human",
            "push",
            "force_push",
            "merge",
            "auto_merge",
            "cleanup",
            "delete_worktree",
            "delete_branch",
        ]:
            self.assertIn(action, self.policy["forbidden_actions"])

    def test_doc_states_runtime_enforcement_is_deferred(self) -> None:
        lower_doc = self.doc.lower().replace("`", "")
        self.assertIn("runtime enforcement is deferred", lower_doc)
        self.assertIn("not enforced by dispatcher", lower_doc)
        self.assertIn("workflow.md remains the human-readable", lower_doc)

    def test_example_lists_deferred_integrations(self) -> None:
        for integration in [
            "github_issues_sync",
            "github_projects",
            "automatic_pr_creation",
            "automatic_merge",
            "remote_worker_pool",
            "multi_host_scheduling",
        ]:
            self.assertIn(integration, self.policy["deferred_integrations"])


if __name__ == "__main__":
    unittest.main()
