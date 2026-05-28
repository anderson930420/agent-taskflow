"""Tests for scripts/run_github_issue_one_task_scheduler_tick.py."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_github_issue_one_task_scheduler_tick.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_github_issue_one_task_scheduler_tick_for_tests",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunGitHubIssueOneTaskSchedulerTickScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.worktree_root = self.root / "worktrees"
        self.lock_path = self.root / "scheduler.lock"
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
            "--lock-path",
            str(self.lock_path),
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
            "run_github_issue_one_task_scheduler_tick",
            side_effect=fake_run,
        ):
            with contextlib.redirect_stdout(stdout):
                rc = self.script.main([*self.base_args(), *extra_args])
        return rc, json.loads(stdout.getvalue()), seen["request"]

    def test_help_flag_succeeds_and_lists_scheduler_flags(self) -> None:
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
            "--lock-path",
            "--operator",
            "--operator-note",
            "--remote",
            "--base-branch",
            "--confirmed",
            "--executor",
            "--validator",
            "--worktree-root",
            "--command",
            "--approved-task-preflight",
            "--skip-approved-task-preflight",
            "--json",
        ):
            self.assertIn(flag, result.stdout, flag)
        for forbidden in (
            "--confirm-ingest-issue",
            "--confirm-run-watcher-one-task",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "--daemon",
            "--cron",
            "--webhook",
            "--batch-size",
            "--merge",
            "--cleanup",
            "--delete-branch",
            "--delete-worktree",
        ):
            self.assertNotIn(forbidden, result.stdout, forbidden)

    def test_script_defaults_to_dry_run_without_confirmed_preset(self) -> None:
        payload = {
            "ok": True,
            "status": "dry_run",
            "mode": "dry_run",
            "safety": {"dry_run": True, "confirmed": False},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            [
                "--issue-limit",
                "25",
                "--include-label",
                "ready",
                "--exclude-label",
                "skip",
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
        self.assertFalse(request.confirmed)
        self.assertEqual(request.issue_limit, 25)
        self.assertEqual(request.include_labels, ("ready",))
        self.assertEqual(request.exclude_labels, ("skip",))
        self.assertEqual(request.lock_path, self.lock_path)
        self.assertTrue(request.fail_if_locked)
        self.assertEqual(request.operator, "codex")
        self.assertEqual(request.operator_note, "dry run check")
        self.assertIsNone(request.executor)

    def test_confirmed_flag_constructs_confirmed_scheduler_request(self) -> None:
        payload = {
            "ok": True,
            "status": "completed_one_task",
            "mode": "confirmed",
            "safety": {"dry_run": False, "confirmed": True},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            [
                "--confirmed",
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
        self.assertTrue(request.confirmed)
        self.assertEqual(request.remote, "upstream")
        self.assertEqual(request.base_branch, "main")
        self.assertIsNone(request.executor)

    def test_confirmed_runner_config_flags_are_passed_to_request(self) -> None:
        payload = {
            "ok": True,
            "status": "completed_one_task",
            "mode": "confirmed",
            "runner_config": {"configured": True, "executor": "shell"},
            "safety": {"dry_run": False, "confirmed": True},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            [
                "--confirmed",
                "--executor",
                "shell",
                "--validator",
                "pytest",
                "--validator",
                "policy",
                "--worktree-root",
                str(self.worktree_root),
                "--command",
                "python -m pytest",
                "--skip-approved-task-preflight",
                "--json",
            ],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(emitted["runner_config"]["executor"], "shell")
        self.assertFalse(request.dry_run)
        self.assertTrue(request.confirmed)
        self.assertEqual(request.executor, "shell")
        self.assertEqual(request.validators, ("pytest", "policy"))
        self.assertEqual(request.worktree_root, self.worktree_root)
        self.assertEqual(request.command, ("python", "-m", "pytest"))
        self.assertFalse(request.approved_task_preflight)

    def test_script_returns_nonzero_for_not_ok_payload(self) -> None:
        payload = {
            "ok": False,
            "status": "automation_error",
            "mode": "confirmed",
            "safety": {"dry_run": False, "confirmed": True},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            ["--confirmed", "--json"],
            payload,
        )

        self.assertEqual(rc, 1)
        self.assertFalse(emitted["ok"])
        self.assertFalse(request.dry_run)
        self.assertTrue(request.confirmed)

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
            "--confirm-ingest-issue",
            "--confirm-run-watcher-one-task",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "--daemon",
            "--cron",
            "--webhook",
            "--batch-size",
            "--merge",
            "--cleanup",
            "--delete-branch",
            "--delete-worktree",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
