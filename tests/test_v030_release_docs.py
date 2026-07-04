"""Doc and metadata tests for the v0.3.0 release."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.3.0-github-release-body.md"


class TestV030ReleaseMetadata(unittest.TestCase):
    def test_pyproject_version_matches_v030_release(self) -> None:
        with PYPROJECT.open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["project"]["version"], "0.3.0")


class TestV030ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")

    def test_release_notes_exist(self) -> None:
        self.assertTrue(RELEASE_NOTES.exists())

    def test_version_string(self) -> None:
        self.assertIn("v0.3.0", self.content)

    def test_release_scope(self) -> None:
        self.assertIn("Claude Code Operator Invocation Runbook", self.content)

    def test_referenced_files(self) -> None:
        self.assertIn(
            "docs/claude-code-operator-invocation-runbook.md", self.content
        )
        self.assertIn(
            "docs/claude-code-bounded-implementer-executor.md", self.content
        )
        self.assertIn(
            "tests/test_claude_code_operator_invocation_runbook.py", self.content
        )

    def test_documented_flags(self) -> None:
        self.assertIn("--executor claude-code", self.content)
        self.assertIn("--claude-code-enable-invocation", self.content)
        self.assertIn("--claude-code-command-json", self.content)
        self.assertIn("--claude-code-timeout-seconds", self.content)

    def test_dry_run_omits_enable_flag(self) -> None:
        self.assertIn(
            "dry-run omits `--claude-code-enable-invocation`", self.content
        )

    def test_real_invocation_requires_opt_in(self) -> None:
        self.assertIn("Real invocation requires explicit opt-in", self.content)

    def test_execution_semantics(self) -> None:
        self.assertIn("The command JSON is argv-based", self.content)
        self.assertIn("no shell parsing", self.content)
        self.assertIn("`shell=True` is not used", self.content)
        self.assertIn(
            "appended as the final argv argument", self.content
        )
        self.assertIn("`cwd` is the prepared worktree", self.content)

    def test_expected_artifacts(self) -> None:
        self.assertIn("claude-code-implementer-prompt.md", self.content)
        self.assertIn("claude-code-execution.json", self.content)
        self.assertIn("claude-code-stdout.log", self.content)
        self.assertIn("claude-code-stderr.log", self.content)

    def test_authority_invariants(self) -> None:
        self.assertIn('validation_authority = "none"', self.content)
        self.assertIn('approval_authority = "none"', self.content)
        self.assertIn('merge_authority = "none"', self.content)
        self.assertIn('cleanup_authority = "none"', self.content)
        self.assertIn("human_review_required = true", self.content)

    def test_pipeline_invariants(self) -> None:
        self.assertIn("Deterministic validators still run", self.content)
        self.assertIn(
            "Codex advisory evidence gate remains authoritative", self.content
        )
        self.assertIn("Human final review remains required", self.content)
        self.assertIn("`waiting_approval` is not approval", self.content)

    def test_no_behavior_change_statements(self) -> None:
        self.assertIn("No executor behavior change", self.content)
        self.assertIn("No runner behavior change", self.content)
        self.assertIn("No registry behavior change", self.content)
        self.assertIn("No scheduler behavior change", self.content)
        self.assertIn("No cron/systemd live profile change", self.content)
        self.assertIn(
            "branch push / PR creation / merge / cleanup / deletion behavior",
            self.content,
        )


if __name__ == "__main__":
    unittest.main()
