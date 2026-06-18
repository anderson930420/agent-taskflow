"""Historical doc tests for the v0.2.5 release.

These tests are historical: they verify that the v0.2.5 release notes still
exist and carry the important v0.2.5 content. They no longer assert that
``pyproject.toml`` is currently ``0.2.5`` — the current version is tracked by the
latest release-doc test (see ``tests/test_v026_release_docs.py``).
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.5-github-release-body.md"


class TestV025ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_version_string(self) -> None:
        self.assertIn("v0.2.5", self.content)

    def test_release_scope(self) -> None:
        self.assertIn(
            "Require Codex Advisory Artifact Evidence Before waiting_approval",
            self.content,
        )
        self.assertIn("required evidence, not Codex approval", self.content)
        self.assertIn("codex_advisory_artifact_contract", self.content)
        self.assertIn("agent_taskflow/codex_advisory_evidence_gate.py", self.content)

    def test_transition_boundary_is_documented(self) -> None:
        self.assertIn("after deterministic validators pass", self.content)
        self.assertIn("before flipping task status to `waiting_approval`", self.content)
        self.assertIn("codex_advisory_evidence", self.content)

    def test_valid_advisory_statuses_do_not_block_by_themselves(self) -> None:
        for status in (
            "looks_good",
            "needs_attention",
            "high_risk",
            "tool_error",
        ):
            self.assertIn(status, self.content)
        self.assertIn("These advisory statuses do not block by themselves", self.content)
        self.assertIn("A valid `high_risk` artifact is required evidence", self.content)
        self.assertIn(
            "A structurally valid `tool_error` artifact is required evidence",
            self.content,
        )

    def test_blocking_cases_are_documented(self) -> None:
        for phrase in (
            "missing",
            "malformed",
            "not a JSON object",
            "task-key mismatched",
            "missing or invalid `review_status`",
            "missing or invalid `risk_level`",
            "missing or non-false `validation_authority`",
            "missing or non-true `human_review_required`",
            "missing required companion artifacts",
            "missing required confirm-run stdout/stderr companions",
            "structurally invalid as `tool_error`",
            "otherwise contract-invalid",
        ):
            self.assertIn(phrase, self.content)

    def test_default_enforcement_and_opt_out_are_documented(self) -> None:
        self.assertIn(
            "ApprovedTaskRunRequest.require_codex_advisory_evidence",
            self.content,
        )
        self.assertIn("defaults to `True`", self.content)
        self.assertIn("explicit opt-out exists", self.content)

    def test_policy_validator_note_is_documented(self) -> None:
        self.assertIn("Policy validator note", self.content)
        self.assertIn("instruction/advisory artifacts", self.content)
        self.assertIn("Secret scanning is still preserved", self.content)

    def test_safety_boundary_is_documented(self) -> None:
        for phrase in (
            "invoke Codex CLI",
            "add subprocess behavior",
            "make Codex judgment validator authority",
            "require `review_status == looks_good`",
            "block merely because `review_status == high_risk`",
            "block merely because `review_status == needs_attention`",
            "block merely because `review_status == tool_error`",
            "change approval authority",
            "change ExecutionEngine authority",
            "change human final approval requirement",
            "push branches",
            "create PRs",
            "merge",
            "cleanup",
            "delete branches",
            "delete worktrees",
            "Claude Code executor",
            "P5-f",
        ):
            self.assertIn(phrase, self.content)


if __name__ == "__main__":
    unittest.main()
