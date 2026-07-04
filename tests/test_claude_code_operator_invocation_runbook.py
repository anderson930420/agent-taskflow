"""Tests for the Claude Code operator invocation runbook (v0.3.0).

Documentation / operator workflow hardening only. These tests assert that the
runbook exists and contains the required safety language and copy-pasteable
commands. They do not exercise any executor, runner, or validator behavior.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "claude-code-operator-invocation-runbook.md"
EXECUTOR_DOC = REPO_ROOT / "docs" / "claude-code-bounded-implementer-executor.md"


class ClaudeCodeOperatorInvocationRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = RUNBOOK.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_runbook_exists(self) -> None:
        self.assertTrue(RUNBOOK.is_file())

    def test_runbook_contains_version_marker(self) -> None:
        self.assertIn("v0.3.0", self.doc)

    def test_runbook_contains_title(self) -> None:
        self.assertIn("Claude Code Operator Invocation Runbook", self.doc)

    def test_runbook_contains_required_cli_flags(self) -> None:
        for flag in (
            "--executor claude-code",
            "--claude-code-enable-invocation",
            "--claude-code-command-json",
            "--claude-code-timeout-seconds",
        ):
            self.assertIn(flag, self.doc)

    def test_runbook_contains_dry_run_prompt_only_guidance(self) -> None:
        self.assertIn("dry-run", self.doc_lower)
        self.assertIn("prompt-only", self.doc_lower)

    def test_runbook_states_dry_run_excludes_enable_flag(self) -> None:
        self.assertIn(
            "does not include `--claude-code-enable-invocation`",
            self.doc_lower,
        )

    def test_runbook_states_real_invocation_requires_explicit_opt_in(self) -> None:
        self.assertIn("real invocation requires", self.doc_lower)
        self.assertIn("explicit opt-in", self.doc_lower)

    def test_runbook_states_command_is_argv_based(self) -> None:
        self.assertIn("argv-based", self.doc_lower)

    def test_runbook_states_no_shell_parsing(self) -> None:
        self.assertIn("no shell parsing", self.doc_lower)

    def test_runbook_states_shell_true_not_used(self) -> None:
        self.assertIn("`shell=true` is not used", self.doc_lower)

    def test_runbook_states_cwd_is_prepared_worktree(self) -> None:
        self.assertIn("cwd is the prepared worktree", self.doc_lower)

    def test_runbook_contains_artifact_filenames(self) -> None:
        for name in (
            "claude-code-execution.json",
            "claude-code-stdout.log",
            "claude-code-stderr.log",
        ):
            self.assertIn(name, self.doc)

    def test_runbook_contains_authority_invariants(self) -> None:
        for invariant in (
            'validation_authority = "none"',
            'approval_authority = "none"',
            'merge_authority = "none"',
            'cleanup_authority = "none"',
            "human_review_required = true",
        ):
            self.assertIn(invariant, self.doc)

    def test_runbook_states_deterministic_validators_still_run(self) -> None:
        self.assertIn("deterministic validators still run", self.doc_lower)

    def test_runbook_states_codex_evidence_gate_authoritative(self) -> None:
        self.assertIn(
            "codex advisory evidence gate remains authoritative",
            self.doc_lower,
        )

    def test_runbook_states_human_final_review_required(self) -> None:
        self.assertIn("human final review remains required", self.doc_lower)

    def test_runbook_states_waiting_approval_is_not_approval(self) -> None:
        self.assertIn("`waiting_approval` is not approval", self.doc_lower)

    def test_runbook_states_no_scheduler_default_change(self) -> None:
        self.assertIn("no scheduler default change", self.doc_lower)

    def test_runbook_states_no_cron_systemd_live_profile_change(self) -> None:
        self.assertIn("no cron/systemd live profile change", self.doc_lower)

    def test_runbook_states_no_push_pr_merge_cleanup_deletion(self) -> None:
        self.assertIn(
            "no branch push / pr creation / merge / cleanup / deletion behavior",
            self.doc_lower,
        )

    def test_runbook_contains_pre_run_checklist(self) -> None:
        self.assertIn("pre-run checklist", self.doc_lower)
        for item in (
            "am i on the correct repo?",
            "is the task explicitly confirmed?",
            "is this a single task?",
            "is `--executor claude-code` intentional?",
            "am i intentionally enabling real invocation?",
            "is the command json a json array of strings?",
            "is timeout finite and positive?",
            "is worktree root controlled?",
            "am i not running this from cron/systemd?",
            "am i not enabling merge/cleanup/delete behavior?",
            "do i understand that `waiting_approval` is not approval?",
        ):
            self.assertIn(item, self.doc_lower)

    def test_runbook_contains_post_run_checklist(self) -> None:
        self.assertIn("post-run checklist", self.doc_lower)
        for item in (
            "did the executor artifact exist?",
            "did stdout/stderr logs exist?",
            "did `claude-code-execution.json` record `invocation_enabled` correctly?",
            "did authority fields remain `none`?",
            "did deterministic validators run?",
            "did the codex advisory evidence gate pass?",
            "is the final state `waiting_approval` rather than approval?",
            "did no push/pr/merge/cleanup/delete occur?",
        ):
            self.assertIn(item, self.doc_lower)

    def test_runbook_uses_placeholder_values(self) -> None:
        for placeholder in (
            "<TASK_KEY>",
            "<REPO_PATH>",
            "<DB_PATH>",
            "<ARTIFACT_ROOT>",
            "<WORKTREE_ROOT>",
        ):
            self.assertIn(placeholder, self.doc)

    def test_runbook_command_json_example_is_argv(self) -> None:
        self.assertIn('["claude", "-p"]', self.doc)

    def test_executor_doc_links_runbook(self) -> None:
        executor_doc = EXECUTOR_DOC.read_text(encoding="utf-8").lower()
        self.assertIn("claude-code-operator-invocation-runbook.md", executor_doc)
        self.assertIn("v0.3.0", executor_doc)
        self.assertIn(
            "operator-facing manual invocation guidance only", executor_doc
        )
        self.assertIn("does not change", executor_doc)


if __name__ == "__main__":
    unittest.main()
