"""Doc and metadata tests for the v0.2.3 release."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.3-github-release-body.md"


class TestV023ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_version_string(self) -> None:
        self.assertIn("v0.2.3", self.content)

    def test_release_scope(self) -> None:
        self.assertIn(
            "Waiting Approval Summary Includes Codex Advisory Review Artifact",
            self.content,
        )
        self.assertIn("codex_advisory_review", self.content)
        self.assertIn("## Codex Advisory Review", self.content)

    def test_artifact_paths_are_documented(self) -> None:
        for artifact in (
            "codex-advisory-review.json",
            "codex-advisory-review.md",
            "codex-advisory-review-stdout.txt",
            "codex-advisory-review-stderr.txt",
        ):
            self.assertIn(artifact, self.content)

    def test_malformed_artifacts_are_warning_only(self) -> None:
        self.assertIn("Malformed", self.content)
        self.assertIn("warning-only", self.lower)
        self.assertIn("do not fail waiting approval summary generation", self.lower)

    def test_advisory_only_authority_boundary(self) -> None:
        self.assertIn("advisory-only", self.lower)
        self.assertIn("non-authoritative", self.lower)
        self.assertIn("validation_authority = false", self.content)
        self.assertIn("human_review_required = true", self.content)

    def test_no_runtime_or_lifecycle_authority_changes(self) -> None:
        for phrase in (
            "invoke Codex CLI",
            "import or call subprocess",
            "scheduler behavior",
            "lifecycle transitions",
            "approval authority",
            "validator authority",
            "ExecutionEngine authority",
            "waiting_approval transition behavior",
            "runtime preflight behavior",
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

    def test_codex_status_does_not_affect_readiness_or_authority(self) -> None:
        for phrase in (
            "looks_good",
            "needs_attention",
            "high_risk",
            "tool_error",
            "ready_for_human_review",
            "validator results",
            "lifecycle status",
            "approval authority",
            "execution authority",
        ):
            self.assertIn(phrase, self.content)


if __name__ == "__main__":
    unittest.main()
