"""Tests for scripts/run_github_issue_one_task_automation.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_github_issue_one_task_automation.py"


def _load_script_module():
    from agent_taskflow.cli import github_issue_one_task_automation as module

    return module


class RunGitHubIssueOneTaskAutomationScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.script = _load_script_module()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def base_args(self) -> list[str]:
        return [
            "--repo",
            "anderson930420/agent-taskflow",
            "--db-path",
            str(self.db_path),
            "--local-repo-path",
            str(self.local_repo),
            "--artifact-root",
            str(self.artifact_root),
        ]

    def invoke_with_fake_run(
        self,
        extra_args: list[str],
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any], Any]:
        seen: dict[str, Any] = {}

        def fake_run(request: Any) -> dict[str, Any]:
            seen["request"] = request
            return payload

        stdout = io.StringIO()
        with mock.patch.object(
            self.script,
            "run_locked_github_issue_one_task_automation",
            side_effect=fake_run,
        ):
            with contextlib.redirect_stdout(stdout):
                rc = self.script.main([*self.base_args(), *extra_args])
        return rc, json.loads(stdout.getvalue()), seen["request"]

    def test_help_flag_succeeds_and_lists_required_flags(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "--repo",
            "--db-path",
            "--local-repo-path",
            "--artifact-root",
            "--issue-limit",
            "--include-label",
            "--exclude-label",
            "--select-first-issue",
            "--confirm-select-first-issue",
            "--confirm-ingest-issue",
            "--confirm-run-watcher-one-task",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "--lock-path",
            "--quarantine-after-ingestion-failures",
            "--operator",
            "--operator-note",
            "--remote",
            "--base-branch",
            "--json",
        ):
            self.assertIn(flag, result.stdout, flag)
        for forbidden in (
            "--daemon",
            "--cron",
            "--webhook",
            "--batch-size",
            "--approve",
            "--merge",
            "--cleanup",
            "--delete-branch",
            "--delete-worktree",
        ):
            self.assertNotIn(forbidden, result.stdout, forbidden)

    def test_script_defaults_to_dry_run_and_outputs_json(self) -> None:
        payload = {
            "ok": True,
            "status": "dry_run",
            "mode": "dry_run",
            "safety": {"dry_run": True},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            [
                "--issue-limit",
                "25",
                "--include-label",
                "ready",
                "--exclude-label",
                "skip",
                "--select-first-issue",
                "--confirm-select-first-issue",
                "--operator",
                "codex",
                "--operator-note",
                "dry run check",
                "--json",
            ],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(emitted["status"], "dry_run")
        self.assertTrue(request.dry_run)
        self.assertEqual(request.issue_limit, 25)
        self.assertEqual(request.include_labels, ("ready",))
        self.assertEqual(request.exclude_labels, ("skip",))
        self.assertTrue(request.select_first_issue)
        self.assertTrue(request.confirm_select_first_issue)
        self.assertFalse(request.confirm_ingest_issue)
        self.assertEqual(request.operator, "codex")
        self.assertEqual(request.operator_note, "dry run check")

    def test_confirm_flags_switch_to_confirmed_mode(self) -> None:
        payload = {
            "ok": True,
            "status": "completed_one_task",
            "mode": "confirmed",
            "safety": {"dry_run": False},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            [
                "--select-first-issue",
                "--confirm-select-first-issue",
                "--confirm-ingest-issue",
                "--confirm-run-watcher-one-task",
                "--confirm-run-one-shot-pipeline",
                "--confirm-prepare-pr",
                "--confirm-github-mutations",
                "--confirm-branch-push",
                "--confirm-draft-pr",
                "--remote",
                "upstream",
                "--base-branch",
                "main",
                "--json",
            ],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(emitted["mode"], "confirmed")
        self.assertFalse(request.dry_run)
        self.assertTrue(request.confirm_ingest_issue)
        self.assertTrue(request.confirm_run_watcher_one_task)
        self.assertTrue(request.confirm_run_one_shot_pipeline)
        self.assertTrue(request.confirm_prepare_pr)
        self.assertTrue(request.confirm_github_mutations)
        self.assertTrue(request.confirm_branch_push)
        self.assertTrue(request.confirm_draft_pr)
        self.assertEqual(request.remote, "upstream")
        self.assertEqual(request.base_branch, "main")

    def test_script_returns_nonzero_for_not_ok_payload(self) -> None:
        payload = {
            "ok": False,
            "status": "confirmation_required",
            "mode": "confirmed",
            "safety": {"dry_run": False},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            ["--confirm-ingest-issue", "--json"],
            payload,
        )

        self.assertEqual(rc, 1)
        self.assertFalse(emitted["ok"])
        self.assertFalse(request.dry_run)

    def test_source_has_no_loop_prompt_or_cleanup_operations(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "Thread(",
            "input(",
            "git push",
            "gh pr create",
            "gh pr merge",
            "merge_pull_request",
            "record_approval_decision(",
            "delete_worktree",
            "git worktree remove",
            "--daemon",
            "--cron",
            "--webhook",
            "--batch-size",
            "--approve",
            "--merge",
            "--cleanup",
            "--delete-branch",
            "--delete-worktree",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
