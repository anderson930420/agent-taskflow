"""Tests for docs/release-readiness-phase-17-25.md governance statements.

These tests verify the release readiness document contains the statements
required by Phase 26 acceptance criteria. They do not execute any smoke run.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DOC = REPO_ROOT / "docs" / "release-readiness-phase-17-25.md"


class ReleaseReadinessDocTests(unittest.TestCase):
    """Verify the release readiness document contains required governance statements."""

    @classmethod
    def setUpClass(cls):
        cls.doc = RELEASE_DOC.read_text(encoding="utf-8")

    # ── Acceptance criterion 4: governance guarantees ─────────────────────

    def test_lists_worker_cannot_approve(self) -> None:
        self.assertIn("approve", self.doc.lower())
        self.assertTrue(
            re.search(r"worker.*not.*approve|worker.*may not.*approve", self.doc, re.IGNORECASE),
            "Doc must state worker cannot approve",
        )

    def test_lists_worker_cannot_push(self) -> None:
        self.assertIn("push", self.doc.lower())
        self.assertTrue(
            re.search(r"worker.*not.*push|worker.*may not.*push", self.doc, re.IGNORECASE),
            "Doc must state worker cannot push",
        )

    def test_lists_worker_cannot_merge(self) -> None:
        self.assertIn("merge", self.doc.lower())
        self.assertTrue(
            re.search(r"worker.*not.*merge|worker.*may not.*merge", self.doc, re.IGNORECASE),
            "Doc must state worker cannot merge",
        )

    def test_lists_worker_cannot_cleanup(self) -> None:
        self.assertIn("cleanup", self.doc.lower())
        self.assertTrue(
            re.search(r"worker.*not.*cleanup|worker.*may not.*cleanup", self.doc, re.IGNORECASE),
            "Doc must state worker cannot cleanup",
        )

    def test_lists_worker_cannot_delete_worktree(self) -> None:
        self.assertIn("delete_worktree", self.doc.lower())

    def test_lists_worker_cannot_delete_branch(self) -> None:
        self.assertIn("delete_branch", self.doc.lower())

    def test_says_deterministic_validators_cannot_be_replaced_by_ai_review(self) -> None:
        self.assertTrue(
            re.search(r"deterministic.*validators.*cannot.*ai|ai.*cannot.*replace.*validators", self.doc, re.IGNORECASE),
            "Doc must state deterministic validators cannot be replaced by AI review",
        )

    def test_says_human_approval_is_final_gate(self) -> None:
        self.assertIn("human approval", self.doc.lower())
        self.assertTrue(
            re.search(r"human approval.*final|final gate.*human", self.doc, re.IGNORECASE),
            "Doc must state human approval is the final gate",
        )

    # ── Acceptance criterion 5: default vs optional validators ────────────────

    def test_states_default_validators(self) -> None:
        self.assertIn("pytest", self.doc.lower())
        self.assertIn("openspec", self.doc.lower())
        self.assertIn(
            "DEFAULT_VALIDATORS",
            self.doc,
            "Doc must state DEFAULT_VALIDATORS value",
        )

    def test_lists_optional_validators(self) -> None:
        for v in ["policy", "typecheck", "lint"]:
            self.assertIn(v, self.doc.lower(), f"Doc must mention {v} as optional validator")

    def test_says_policy_typecheck_lint_are_opt_in(self) -> None:
        self.assertTrue(
            re.search(r"opt-in|optional", self.doc.lower()),
            "Doc must clarify policy/typecheck/lint are opt-in",
        )

    def test_states_policy_validator_does_not_call_ai(self) -> None:
        self.assertIn("policy", self.doc.lower())
        self.assertTrue(
            re.search(r"policy.*not.*call|policy.*no.*network|policy.*pure", self.doc, re.IGNORECASE),
            "Doc must state policy validator does not call AI",
        )

    # ── Acceptance criterion 6: Mission Control is read-only ─────────────────

    def test_says_mission_control_api_is_read_only(self) -> None:
        self.assertTrue(
            re.search(r"read-only", self.doc, re.IGNORECASE),
            "Doc must state Mission Control API is read-only",
        )

    def test_says_mission_control_frontend_is_read_only(self) -> None:
        self.assertTrue(
            re.search(r"frontend.*read-only|no approval actions", self.doc, re.IGNORECASE),
            "Doc must state Mission Control frontend is read-only",
        )

    def test_mentions_path_traversal_blocked(self) -> None:
        self.assertIn("path traversal", self.doc.lower())

    def test_mentions_secret_redaction(self) -> None:
        self.assertIn("secret", self.doc.lower())
        self.assertIn("redact", self.doc.lower())

    # ── Acceptance criterion 7: Pi is executor backend ───────────────────────

    def test_states_pi_is_executor_backend(self) -> None:
        self.assertTrue(
            re.search(r"pi.*executor.*backend|executor.*backend.*pi", self.doc, re.IGNORECASE),
            "Doc must state Pi is an executor backend",
        )

    def test_says_pi_never_self_validates(self) -> None:
        self.assertTrue(
            re.search(r"pi.*not.*self.*validat|pi.*does not.*self.*validat", self.doc, re.IGNORECASE),
            "Doc must state Pi does not self-validate",
        )

    def test_says_pi_never_self_approves(self) -> None:
        self.assertTrue(
            re.search(r"pi.*not.*self.*approv|pi.*never.*approv", self.doc, re.IGNORECASE),
            "Doc must state Pi does not self-approve",
        )

    def test_says_pi_never_pushes_or_merges(self) -> None:
        self.assertTrue(
            re.search(r"pi.*not.*push|pi.*not.*merge|pi.*never.*push", self.doc, re.IGNORECASE),
            "Doc must state Pi does not push or merge",
        )

    # ── Acceptance criterion 8: next steps listed ──────────────────────────

    def test_lists_option_a_manual_smoke_run(self) -> None:
        self.assertIn("Option A", self.doc)

    def test_lists_option_b_merge_to_main(self) -> None:
        self.assertIn("Option B", self.doc)

    def test_lists_recommended_next_steps(self) -> None:
        self.assertTrue(
            re.search(r"Option [ABC]|[Nn]ext [Ss]tep|[Rr]ecommend", self.doc),
            "Doc must list recommended next steps",
        )

    # ── Acceptance criterion 2: Phase 17–25 summary ─────────────────────────

    def test_lists_phase_17_commit(self) -> None:
        self.assertIn("a8527ac", self.doc)

    def test_lists_phase_18_commit(self) -> None:
        self.assertIn("5953720", self.doc)

    def test_lists_phase_19_commit(self) -> None:
        self.assertIn("70bd986", self.doc)

    def test_lists_phase_20_commit(self) -> None:
        self.assertIn("183e839", self.doc)

    def test_lists_phase_21_commit(self) -> None:
        self.assertIn("4758f80", self.doc)

    def test_lists_phase_22_commit(self) -> None:
        self.assertIn("f7b9c4b", self.doc)

    def test_lists_phase_23_commit(self) -> None:
        self.assertIn("ed01d89", self.doc)

    def test_lists_phase_24_commit(self) -> None:
        self.assertIn("82f73c8", self.doc)

    def test_lists_phase_25_commit(self) -> None:
        self.assertIn("d978634", self.doc)

    def test_includes_commit_chain_table(self) -> None:
        self.assertTrue(
            re.search(r"Phase.*Commit.*Subject", self.doc),
            "Doc must include Phase 17–25 commit chain table",
        )

    # ── Acceptance criterion 3: architecture summary ───────────────────────

    def test_includes_architecture_diagram_text(self) -> None:
        self.assertIn("TaskRecord", self.doc)
        self.assertIn("mission_contract.json", self.doc)
        self.assertIn("pi_mission_plan.json", self.doc)
        self.assertIn("pi-executor.log", self.doc)
        self.assertIn("policy-validate.log", self.doc)

    def test_lists_artifacts_table(self) -> None:
        self.assertTrue(
            re.search(r"Artifact.*Written By|artifact.*table", self.doc, re.IGNORECASE),
            "Doc must list artifacts produced per run",
        )

    def test_mentions_deterministic_validators(self) -> None:
        self.assertIn("deterministic validators", self.doc.lower())

    def test_mentions_human_approval_gate(self) -> None:
        self.assertIn("waiting_approval", self.doc)
        self.assertIn("human approval", self.doc.lower())

    # ── Acceptance criterion 1: release readiness document exists ───────────

    def test_has_release_readiness_checklist(self) -> None:
        self.assertIn("checklist", self.doc.lower())
        self.assertIn("git status", self.doc.lower())

    def test_has_no_db_schema_change_statement(self) -> None:
        self.assertTrue(
            re.search(r"no.*db.*schema.*change|db.*schema.*unchanged", self.doc, re.IGNORECASE),
            "Doc must state no DB schema change",
        )

    def test_has_no_approval_semantics_change_statement(self) -> None:
        self.assertTrue(
            re.search(r"no.*approval.*semantic.*change|approval.*semantic.*unchanged", self.doc, re.IGNORECASE),
            "Doc must state no approval semantic change",
        )

    def test_has_no_destructive_ui_action_statement(self) -> None:
        self.assertTrue(
            re.search(r"no.*destructive.*ui|destructive.*ui.*action", self.doc, re.IGNORECASE),
            "Doc must state no destructive UI action",
        )

    def test_has_no_default_validator_change_statement(self) -> None:
        self.assertTrue(
            re.search(r"no.*default.*validator.*change|default.*validator.*unchanged", self.doc, re.IGNORECASE),
            "Doc must state no default validator change",
        )

    # ── Smoke procedure reference ───────────────────────────────────────────

    def test_references_pi_governance_smoke_doc(self) -> None:
        self.assertIn("pi-governance-e2e-smoke", self.doc)

    # ── Mission contract section ────────────────────────────────────────────

    def test_documents_mission_contract_schema(self) -> None:
        self.assertIn("mission_contract.json", self.doc)
        self.assertIn("forbidden_actions", self.doc)

    # ── Pi mission plan section ──────────────────────────────────────────────

    def test_documents_pi_mission_plan(self) -> None:
        self.assertIn("pi_mission_plan.json", self.doc)
        self.assertIn("scout", self.doc)
        self.assertIn("planner", self.doc)
        self.assertIn("implementer", self.doc)
        self.assertIn("reviewer", self.doc)
        self.assertIn("handoff", self.doc)

    # ── Pi mission prompt section ────────────────────────────────────────────

    def test_documents_pi_mission_prompt(self) -> None:
        self.assertIn("pi_mission_prompt.md", self.doc)


if __name__ == "__main__":
    unittest.main()