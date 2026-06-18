"""Doc and metadata tests for the v0.2.4 release."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.4-github-release-body.md"


class TestV024ReleaseMetadata(unittest.TestCase):
    def test_pyproject_version_matches_v024_release(self) -> None:
        with PYPROJECT.open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["project"]["version"], "0.2.4")


class TestV024ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_version_string(self) -> None:
        self.assertIn("v0.2.4", self.content)

    def test_release_scope(self) -> None:
        self.assertIn("Codex Advisory Artifact Contract Validator", self.content)
        self.assertIn("codex_advisory_artifact_contract", self.content)
        self.assertIn(
            "agent_taskflow/codex_advisory_artifact_contract_validator.py",
            self.content,
        )

    def test_contract_shape_not_advisory_judgment(self) -> None:
        self.assertIn("validates artifact contract shape only", self.lower)
        self.assertIn("does not judge Codex's advisory review content", self.content)

    def test_contract_checks_are_documented(self) -> None:
        for phrase in (
            "codex-advisory-review.json",
            "JSON parses as an object",
            "schema_version",
            "reviewer",
            "task_key",
            "review_status",
            "risk_level",
            "validation_authority",
            "human_review_required",
            "codex-advisory-review.md",
            "stdout/stderr",
            "tool_error",
            "generated_at",
        ):
            self.assertIn(phrase, self.content)

    def test_valid_advisory_statuses_do_not_fail_by_themselves(self) -> None:
        for status in (
            "looks_good",
            "needs_attention",
            "high_risk",
            "tool_error",
        ):
            self.assertIn(status, self.content)
        self.assertIn("does not fail merely because Codex reports", self.content)
        self.assertIn("`tool_error` passes when it is structurally valid", self.content)

    def test_fail_semantics_are_documented(self) -> None:
        for phrase in (
            "missing `codex-advisory-review.json`",
            "malformed JSON",
            "JSON that is not an object",
            "missing or invalid schema",
            "missing or mismatched `task_key`",
            "missing or invalid `review_status`",
            "missing or invalid `risk_level`",
            "missing or non-false `validation_authority`",
            "missing or non-true `human_review_required`",
            "missing markdown companion artifact",
            "missing required confirm-run stdout/stderr companions",
            "structurally invalid `tool_error`",
        ):
            self.assertIn(phrase, self.content)

    def test_no_scheduler_runner_or_lifecycle_changes(self) -> None:
        for phrase in (
            "invoke Codex CLI",
            "import or call subprocess",
            "wire the validator into scheduler / runner required evidence flow",
            "require Codex artifacts before `waiting_approval`",
            "change `waiting_approval` transition behavior",
            "change v0.2.3 waiting approval summary behavior",
            "change `ready_for_human_review`",
            "change approval authority",
            "change validator authority",
            "change ExecutionEngine authority",
            "change runtime preflight behavior",
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
