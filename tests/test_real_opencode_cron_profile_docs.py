"""Tests for the cautious real opencode cron profile example and docs."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CRON_EXAMPLE = (
    REPO_ROOT
    / "deploy"
    / "cron"
    / "github-issue-one-task-real-opencode.cron.example"
)
DOC = REPO_ROOT / "docs" / "github-issue-one-task-real-cron-profile.md"


class RealOpenCodeCronProfileExampleTests(unittest.TestCase):
    def test_cron_example_exists(self) -> None:
        self.assertTrue(CRON_EXAMPLE.exists())

    def test_cron_example_contains_required_flags(self) -> None:
        text = CRON_EXAMPLE.read_text(encoding="utf-8")
        for needle in (
            "scripts/run_github_issue_one_task_scheduler_tick.py",
            "--confirmed",
            "--executor opencode",
            "--model",
            "minimax-coding-plan/MiniMax-M2.7",
            "--validator policy",
            "--include-observability-summary",
            "--json",
            "logs/github-issue-one-task-real-opencode.jsonl",
        ):
            self.assertIn(needle, text, f"cron example missing {needle!r}")

    def test_cron_command_line_includes_observability_flag(self) -> None:
        # The flag must be on the actual scheduler tick command line, not only
        # in a comment, so the documented example really opts into the summary.
        text = CRON_EXAMPLE.read_text(encoding="utf-8")
        command_lines = [
            line
            for line in text.splitlines()
            if "run_github_issue_one_task_scheduler_tick.py" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertTrue(command_lines, "no active scheduler tick command line")
        for line in command_lines:
            self.assertIn("--include-observability-summary", line)
            self.assertIn("--json", line)

    def test_cron_example_runs_every_30_minutes(self) -> None:
        text = CRON_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("*/30 * * * *", text)

    def test_cron_example_omits_publication_and_destructive_tokens(self) -> None:
        text = CRON_EXAMPLE.read_text(encoding="utf-8")
        for forbidden in (
            "--publish-after-execution",
            "gh pr merge",
            "gh pr create",
            "git push",
            "cleanup",
            "delete_branch",
            "delete_worktree",
        ):
            self.assertNotIn(
                forbidden, text, f"cron example must not contain {forbidden!r}"
            )


class RealOpenCodeCronProfileDocTests(unittest.TestCase):
    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.exists())

    def test_doc_names_execution_only_and_safety_boundaries(self) -> None:
        text = DOC.read_text(encoding="utf-8").lower()
        for phrase in (
            "execution-only",
            "no auto-merge",
            "no auto-approval",
            "no cleanup",
        ):
            self.assertIn(phrase, text, f"doc missing {phrase!r}")

    def test_doc_references_cron_example(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        self.assertIn(
            "deploy/cron/github-issue-one-task-real-opencode.cron.example",
            text,
        )

    def test_doc_documents_observability_summary_opt_in(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("--include-observability-summary", text)
        self.assertIn("observability_summary", text)

    def test_doc_points_to_active_cron_rollout_runbook(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("docs/active-cron-observability-rollout.md", text)

    def test_doc_states_observability_safety_boundaries(self) -> None:
        raw = DOC.read_text(encoding="utf-8").lower()
        # Strip markdown emphasis/backticks so phrase assertions are robust.
        normalized = re.sub(r"\s+", " ", raw.replace("*", "").replace("`", ""))
        # active crontab is not modified
        self.assertIn("does not modify your active crontab", normalized)
        # scheduler tick is not migrated to ExecutionEngine
        self.assertIn(
            "scheduler tick is not migrated to executionengine", normalized
        )
        # old logs without the summary still fall back
        self.assertIn("legacy fallback", normalized)


if __name__ == "__main__":
    unittest.main()
