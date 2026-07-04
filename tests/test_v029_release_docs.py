"""Historical doc tests for the v0.2.9 release.

These tests are historical: they verify that the v0.2.9 release notes still
exist and carry the important v0.2.9 content. They no longer assert that
``pyproject.toml`` is currently ``0.2.9`` -- the current version is tracked by
the latest release-doc test (see ``tests/test_v030_release_docs.py``).
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.9-github-release-body.md"


class TestV029ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_release_notes_exist(self) -> None:
        self.assertTrue(RELEASE_NOTES.exists())

    def test_version_string(self) -> None:
        self.assertIn("v0.2.9", self.content)

    def test_release_scope(self) -> None:
        self.assertIn(
            "Claude Code Real Invocation Workflow Policy + Golden Path Smoke",
            self.content,
        )
        self.assertIn("claude-code", self.content)
        self.assertIn("allowed_executors", self.content)

    def test_workflow_policy_alignment(self) -> None:
        self.assertIn(
            "explicitly selectable bounded implementer executor",
            self.content,
        )
        self.assertIn("`claude-code` is not the default executor", self.content)
        self.assertIn(
            "is not added to the canonical `allowed_executors` example",
            self.content,
        )
        self.assertIn('list `"claude-code"` in `allowed_executors`', self.content)

    def test_smoke_test_names_documented(self) -> None:
        self.assertIn(
            "tests/test_approved_task_runner.py::ApprovedTaskRunnerTests::"
            "test_claude_code_real_invocation_golden_path_smoke",
            self.content,
        )
        self.assertIn(
            "tests/test_run_approved_task_script.py::RunApprovedTaskScriptTests::"
            "test_run_approved_task_claude_code_real_invocation_golden_path_smoke",
            self.content,
        )

    def test_smoke_environment_is_safe(self) -> None:
        self.assertIn("fake argv commands", self.content)
        self.assertIn("No real Claude Code is invoked in tests", self.content)

    def test_smoke_execution_semantics(self) -> None:
        self.assertIn("argv-based, not shell-string based", self.content)
        self.assertIn("`cwd` is the prepared worktree", self.content)
        self.assertIn("claude-code-execution.json", self.content)
        self.assertIn("claude_code_executor.v1", self.content)
        self.assertIn("invocation_enabled = true", self.content)

    def test_smoke_authority_invariants(self) -> None:
        self.assertIn("authority fields remain `none`", self.content)
        self.assertIn("human_review_required = true", self.content)

    def test_smoke_pipeline_invariants(self) -> None:
        self.assertIn(
            "Deterministic validators still run after the executor",
            self.content,
        )
        self.assertIn(
            "successful invocation without Codex advisory evidence remains blocked",
            self.content,
        )
        self.assertIn(
            "successful invocation with valid Codex advisory evidence can reach",
            self.content,
        )
        self.assertIn("`waiting_approval` is not approval", self.content)

    def test_no_behavior_change_statements(self) -> None:
        self.assertIn("No executor behavior change", self.content)
        self.assertIn("No runner behavior change", self.content)
        self.assertIn("No scheduler default change", self.content)
        self.assertIn("No cron/systemd live profile change", self.content)

    def test_governance_invariants(self) -> None:
        self.assertIn(
            "Codex advisory evidence gate remains authoritative",
            self.content,
        )
        self.assertIn("Human final review remains required", self.content)


if __name__ == "__main__":
    unittest.main()
