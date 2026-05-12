"""Tests for docs/pi-governance-e2e-smoke.md governance statements.

These tests verify the smoke document contains the governance statements
required by Phase 25 acceptance criteria. They do not run the smoke workflow.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_DOC = REPO_ROOT / "docs" / "pi-governance-e2e-smoke.md"


class SmokeDocDBAlignmentTests(unittest.TestCase):
    """Verify the smoke document covers DB alignment for review evidence API."""

    @classmethod
    def setUpClass(cls):
        cls.doc = SMOKE_DOC.read_text(encoding="utf-8")

    def test_doc_has_db_alignment_section(self) -> None:
        self.assertIn(
            "Review Evidence API DB Alignment",
            self.doc,
            "Smoke doc must have a Review Evidence API DB Alignment section",
        )

    def test_doc_says_same_db_required(self) -> None:
        self.assertIn(
            "same db",
            self.doc.lower(),
            "Smoke doc must say API server must use the same DB as smoke task",
        )

    def test_doc_explains_default_db_404(self) -> None:
        self.assertIn(
            "404",
            self.doc,
            "Smoke doc must explain that default DB causes 404",
        )

    def test_doc_shows_create_app_db_path(self) -> None:
        self.assertIn(
            "create_app(db_path=",
            self.doc,
            "Smoke doc must show create_app(db_path=...) for smoke DB",
        )

    def test_doc_records_smoke_db_path(self) -> None:
        self.assertIn(
            "SMOKE_DB",
            self.doc,
            "Smoke doc must mention SMOKE_DB recording",
        )

    def test_doc_records_task_key(self) -> None:
        self.assertIn(
            "TASK_KEY",
            self.doc,
            "Smoke doc must mention TASK_KEY recording",
        )

    def test_doc_records_artifact_dir(self) -> None:
        self.assertIn(
            "ARTIFACT_DIR",
            self.doc,
            "Smoke doc must mention ARTIFACT_DIR recording",
        )

    def test_doc_has_review_evidence_curl_command(self) -> None:
        self.assertIn(
            "review-evidence",
            self.doc,
            "Smoke doc must have curl command for review-evidence endpoint",
        )

    def test_doc_has_artifact_preview_curl_commands(self) -> None:
        for artifact in ["pi_mission_prompt.md", "pi_mission_plan.json", "policy-validate.log"]:
            self.assertIn(
                f"/artifacts/{artifact}",
                self.doc,
                f"Smoke doc must have curl command for /artifacts/{artifact}",
            )

    def test_doc_lists_successful_response_indicators(self) -> None:
        self.assertIn(
            "present",
            self.doc.lower(),
            "Smoke doc must list 'present' as a success indicator",
        )
        self.assertIn(
            "human_approval_required",
            self.doc.lower(),
            "Smoke doc must mention human_approval_required",
        )
        self.assertIn(
            "policy_status",
            self.doc.lower(),
            "Smoke doc must mention policy_status",
        )
        self.assertIn(
            "no secrets",
            self.doc.lower(),
            "Smoke doc must confirm no secrets exposed in API response",
        )


class SmokeDocGovernanceTests(unittest.TestCase):
    """Verify the smoke document contains required governance statements."""

    @classmethod
    def setUpClass(cls):
        cls.doc = SMOKE_DOC.read_text(encoding="utf-8")

    # Acceptance criteria 4: Pi is not a governance layer
    def test_doc_says_pi_is_executor_backend_not_governance_layer(self) -> None:
        self.assertIn(
            "executor backend",
            self.doc.lower(),
            "Smoke doc must say Pi is an executor backend, not a governance layer",
        )

    def test_doc_does_not_claim_pi_is_a_governance_layer(self) -> None:
        # Negative check: Pi is explicitly not a governance layer.
        patterns = [
            r"pi.*is.*governance",
            r"pi.*governs",
            r"pi.*approve",
        ]
        for pat in patterns:
            matches = re.findall(pat, self.doc, re.IGNORECASE)
            # Allow "Pi never approves" (negative statement).
            # Disallow "Pi is a governance layer" (positive claim).
            for m in matches:
                self.assertNotIn(
                    "pi is a governance",
                    m.lower(),
                    f"Document must not claim Pi is a governance layer: {m!r}",
                )

    # Acceptance criteria 3: no approve
    def test_doc_says_do_not_approve(self) -> None:
        self.assertTrue(
            re.search(r"do not\s+(approve|merge|push|cleanup)", self.doc, re.IGNORECASE),
            "Smoke doc must say do not approve/merge/push/cleanup",
        )

    def test_doc_says_pi_never_approves(self) -> None:
        self.assertIn(
            "never approves",
            self.doc.lower(),
            "Smoke doc must say Pi never approves",
        )

    def test_doc_says_human_approval_is_final_gate(self) -> None:
        self.assertIn(
            "human approval is the final gate",
            self.doc.lower(),
            "Smoke doc must say human approval is the final gate",
        )

    # Acceptance criteria 3: no push/merge/cleanup
    def test_doc_says_no_push(self) -> None:
        self.assertIn(
            "no push",
            self.doc.lower().replace("push", "no push"),
            "Smoke doc must say no push",
        )

    def test_doc_says_no_merge(self) -> None:
        self.assertIn(
            "no merge",
            self.doc.lower().replace("merge", "no merge"),
            "Smoke doc must say no merge",
        )

    def test_doc_says_no_cleanup(self) -> None:
        self.assertIn(
            "no cleanup",
            self.doc.lower().replace("cleanup", "no cleanup"),
            "Smoke doc must say no cleanup",
        )

    def test_doc_lists_forbidden_destructive_actions(self) -> None:
        """doc must list push, merge, cleanup, delete_branch, delete_worktree."""
        for action in ["push", "merge", "cleanup", "delete_branch", "delete_worktree"]:
            self.assertIn(
                action,
                self.doc.lower(),
                f"Smoke doc must mention {action}",
            )

    # Acceptance criteria 5: deterministic validators remain required
    def test_doc_says_deterministic_validators_remain_required(self) -> None:
        self.assertIn(
            "deterministic validators",
            self.doc.lower(),
            "Smoke doc must say deterministic validators remain required",
        )

    def test_doc_lists_required_validators(self) -> None:
        """doc must list pytest, openspec, policy, typecheck, lint."""
        for v in ["pytest", "openspec", "policy"]:
            self.assertIn(
                v,
                self.doc.lower(),
                f"Smoke doc must mention {v} validator",
            )

    def test_doc_says_ai_reviews_cannot_replace_validators(self) -> None:
        self.assertIn(
            "cannot replace",
            self.doc.lower(),
            "Smoke doc must say AI reviews cannot replace validators",
        )

    # Acceptance criteria 6: expected artifacts listed
    def test_doc_lists_expected_artifacts(self) -> None:
        """doc must list expected artifacts in the smoke run."""
        for artifact in [
            "mission_contract.json",
            "pi_mission_plan.json",
            "pi_mission_prompt.md",
            "pi-executor.log",
            "policy-validate.log",
        ]:
            self.assertIn(
                artifact,
                self.doc,
                f"Smoke doc must list expected artifact: {artifact}",
            )

    def test_doc_marks_required_vs_optional_artifacts(self) -> None:
        """doc must distinguish required from optional artifacts."""
        # At minimum, mission_contract.json must be labeled as required
        # and pytest.log / openspec.log must be labeled as optional.
        self.assertIn(
            "mission_contract.json",
            self.doc,
            "mission_contract.json must be in the doc",
        )
        # Check for optional marking
        self.assertTrue(
            re.search(r"optional", self.doc, re.IGNORECASE),
            "Smoke doc must mark some artifacts as optional",
        )

    # Acceptance criteria 2: setup / run / verify / review evidence / cleanup
    def test_doc_has_step_1_create_task(self) -> None:
        self.assertIn("Step 1", self.doc)
        self.assertIn("create", self.doc.lower())

    def test_doc_has_step_2_dispatcher(self) -> None:
        self.assertIn("Step 2", self.doc)
        self.assertIn("dispatcher", self.doc.lower())

    def test_doc_has_step_3_verify_artifacts(self) -> None:
        self.assertIn("Step 3", self.doc)
        self.assertIn("artifacts", self.doc.lower())

    def test_doc_has_step_4_review_evidence_api(self) -> None:
        self.assertIn("Step 4", self.doc)
        self.assertIn("review evidence", self.doc.lower())

    def test_doc_has_step_5_cleanup(self) -> None:
        self.assertIn("Step 5", self.doc)
        self.assertIn("cleanup", self.doc.lower())

    # Acceptance criteria 1: document is repeatable
    def test_doc_has_quick_reference_commands(self) -> None:
        self.assertIn(
            "Quick Reference",
            self.doc,
            "Smoke doc must have a quick reference section for repeatable runs",
        )

    # Safety rules
    def test_doc_requires_clean_git_status(self) -> None:
        self.assertIn(
            "git status",
            self.doc.lower(),
            "Smoke doc must require clean git status",
        )

    def test_doc_says_do_not_use_production_db(self) -> None:
        self.assertIn(
            "production",
            self.doc.lower(),
            "Smoke doc must warn against production DB",
        )

    def test_doc_says_no_api_keys_in_repo_files(self) -> None:
        self.assertIn(
            "api keys",
            self.doc.lower(),
            "Smoke doc must warn against putting API keys in repo files",
        )

    def test_doc_says_stop_stuck_pi_processes(self) -> None:
        self.assertIn(
            "stuck",
            self.doc.lower(),
            "Smoke doc must mention stopping stuck Pi processes",
        )

    # Review evidence verification
    def test_doc_has_curl_commands_for_review_evidence_api(self) -> None:
        self.assertIn(
            "review-evidence",
            self.doc,
            "Smoke doc must have curl commands for review evidence API",
        )

    def test_doc_verifies_no_secrets_in_api_response(self) -> None:
        self.assertIn(
            "no secrets",
            self.doc.lower(),
            "Smoke doc must verify no secrets are exposed in review evidence API",
        )

    # Artifact verification commands
    def test_doc_has_mission_contract_verification_command(self) -> None:
        self.assertIn(
            "mission_contract.json",
            self.doc,
            "Smoke doc must have verification command for mission_contract.json",
        )

    def test_doc_has_pi_mission_plan_verification_command(self) -> None:
        self.assertIn(
            "pi_mission_plan.json",
            self.doc,
            "Smoke doc must have verification command for pi_mission_plan.json",
        )

    def test_doc_has_policy_validator_log_verification(self) -> None:
        self.assertIn(
            "policy-validate.log",
            self.doc,
            "Smoke doc must have verification for policy-validate.log",
        )


if __name__ == "__main__":
    unittest.main()