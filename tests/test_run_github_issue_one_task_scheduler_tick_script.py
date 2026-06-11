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
    from agent_taskflow.cli import github_issue_one_task_scheduler_tick as module

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

    def scheduler_tick_payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "schema_version": "github_issue_one_task_scheduler_tick.v1",
            "source": "github_issue_one_task_scheduler_tick",
            "status": "execution_completed",
            "mode": "confirmed",
            "repo": "anderson930420/agent-taskflow",
            "lock": {
                "path": str(self.lock_path),
                "acquired": True,
                "contended": False,
                "released": True,
            },
            "runner_config": {
                "configured": True,
                "executor": "pi",
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
                "tools": ["read", "write"],
                "validators": ["pytest", "changed-files"],
            },
            "publication_config": {
                "publish_after_execution": False,
                "mode": "execution_only",
                "next_operator_action": "run explicit task-to-draft-pr workflow",
            },
            "selected_task_key": "AT-GH-900",
            "selected_issue": {"number": 900, "title": "Scheduler tick test"},
            "safety": {
                "scheduled_tick": True,
                "one_tick_only": True,
                "one_issue_only": True,
                "one_task_only": True,
                "dry_run": False,
                "confirmed": True,
                "github_mutated": False,
                "approved": False,
                "merged": False,
                "cleanup_performed": False,
                "branch_deleted": False,
                "worktree_deleted": False,
                "scheduler_loop_started": False,
                "background_worker_started": False,
                "multi_task_batch_started": False,
                "human_review_required": True,
            },
        }
        payload.update(overrides)
        return payload

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
            "--publish-after-execution",
            "--executor",
            "--validator",
            "--worktree-root",
            "--command",
            "--approved-task-preflight",
            "--skip-approved-task-preflight",
            "--json",
            "--include-observability-summary",
            "--observability-summary-only",
            "--use-execution-engine",
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
        self.assertEqual(emitted, payload)
        self.assertEqual(emitted["status"], "dry_run")
        self.assertNotIn("observability_summary", emitted)
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

    def test_help_lists_executor_profile_flags(self) -> None:
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
        for flag in ("--model", "--provider", "--tools", "--pi-bin"):
            self.assertIn(flag, result.stdout, flag)

    def test_executor_profile_flags_parse_into_request(self) -> None:
        payload = {
            "ok": True,
            "status": "dry_run",
            "mode": "dry_run",
            "safety": {"dry_run": True, "confirmed": False},
        }

        rc, _emitted, request = self.invoke_with_fake_run(
            [
                "--model",
                "claude-sonnet-4-6",
                "--provider",
                "anthropic",
                "--tools",
                "read",
                "--tools",
                "write",
                "--pi-bin",
                "pi",
                "--json",
            ],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(request.model, "claude-sonnet-4-6")
        self.assertEqual(request.provider, "anthropic")
        self.assertEqual(request.tools, ("read", "write"))
        self.assertEqual(request.pi_bin, "pi")

    def test_executor_profile_flags_default_to_none(self) -> None:
        payload = {
            "ok": True,
            "status": "dry_run",
            "mode": "dry_run",
            "safety": {"dry_run": True, "confirmed": False},
        }

        rc, _emitted, request = self.invoke_with_fake_run(["--json"], payload)

        self.assertEqual(rc, 0)
        self.assertIsNone(request.model)
        self.assertIsNone(request.provider)
        self.assertIsNone(request.tools)
        self.assertIsNone(request.pi_bin)

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

    def test_confirmed_defaults_to_execution_only(self) -> None:
        payload = {
            "ok": True,
            "status": "execution_completed",
            "mode": "confirmed",
            "publication_config": {
                "publish_after_execution": False,
                "mode": "execution_only",
            },
            "safety": {"dry_run": False, "confirmed": True},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            ["--confirmed", "--json"],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(emitted["publication_config"]["mode"], "execution_only")
        self.assertTrue(request.confirmed)
        self.assertFalse(request.publish_after_execution)

    def test_publish_after_execution_flag_is_passed_to_request(self) -> None:
        payload = {
            "ok": True,
            "status": "completed_one_task",
            "mode": "confirmed",
            "publication_config": {
                "publish_after_execution": True,
                "mode": "publication",
            },
            "safety": {"dry_run": False, "confirmed": True},
        }

        rc, emitted, request = self.invoke_with_fake_run(
            ["--confirmed", "--publish-after-execution", "--json"],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(emitted["publication_config"]["mode"], "publication")
        self.assertTrue(request.confirmed)
        self.assertTrue(request.publish_after_execution)

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

    def test_include_observability_summary_json_emits_existing_payload_plus_summary(
        self,
    ) -> None:
        payload = self.scheduler_tick_payload()

        rc, emitted, request = self.invoke_with_fake_run(
            ["--include-observability-summary", "--json"],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertFalse(request.confirmed)
        self.assertIn("observability_summary", emitted)
        summary = emitted["observability_summary"]
        without_summary = {
            key: value for key, value in emitted.items() if key != "observability_summary"
        }
        self.assertEqual(without_summary, payload)
        self.assertEqual(
            summary["schema_version"], "execution_observability_summary.v1"
        )
        self.assertEqual(summary["source"], "scheduler_tick")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["status"], payload["status"])
        self.assertEqual(summary["task_key"], payload["selected_task_key"])
        self.assertEqual(summary["mode"], payload["mode"])
        self.assertEqual(summary["profile"]["executor"], "pi")
        self.assertEqual(summary["profile"]["model"], "claude-sonnet-4-6")
        self.assertEqual(summary["profile"]["validators"], ["pytest", "changed-files"])
        self.assertEqual(summary["publication_mode"], "execution_only")
        self.assertTrue(summary["safety"]["human_review_required"])
        self.assertTrue(summary["safety"]["one_task_only"])
        self.assertTrue(summary["safety"]["execution_only"])
        self.assertFalse(summary["safety"]["approved"])
        self.assertFalse(summary["safety"]["merged"])
        self.assertFalse(summary["safety"]["github_mutated"])
        self.assertFalse(summary["safety"]["branch_deleted"])
        self.assertFalse(summary["safety"]["worktree_deleted"])

    def test_observability_summary_only_json_emits_only_summary(self) -> None:
        payload = self.scheduler_tick_payload()

        rc, emitted, _request = self.invoke_with_fake_run(
            ["--observability-summary-only", "--json"],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(
            set(emitted),
            {
                "schema_version",
                "source",
                "ok",
                "task_key",
                "status",
                "raw_status",
                "dry_run",
                "mode",
                "publication_mode",
                "next_operator_action",
                "profile",
                "safety",
                "steps",
                "artifacts",
                "metadata",
            },
        )
        self.assertEqual(
            emitted["schema_version"], "execution_observability_summary.v1"
        )
        self.assertEqual(emitted["source"], "scheduler_tick")
        self.assertEqual(emitted["status"], payload["status"])
        self.assertEqual(emitted["profile"]["executor"], "pi")
        self.assertNotIn("observability_summary", emitted)
        self.assertNotIn("runner_config", emitted)
        self.assertNotIn("publication_config", emitted)

    def test_observability_summary_only_implies_json_output(self) -> None:
        payload = self.scheduler_tick_payload()

        rc, emitted, _request = self.invoke_with_fake_run(
            ["--observability-summary-only"],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(
            emitted["schema_version"], "execution_observability_summary.v1"
        )
        self.assertEqual(emitted["source"], "scheduler_tick")
        self.assertNotIn("runner_config", emitted)

    def test_no_eligible_issues_payload_can_be_summarized(self) -> None:
        payload = self.scheduler_tick_payload(
            status="no_eligible_issues",
            selected_task_key=None,
            selected_issue=None,
            runner_config={"configured": False, "executor": None, "validators": []},
            publication_config={
                "publish_after_execution": False,
                "mode": "execution_only",
                "next_operator_action": "run explicit task-to-draft-pr workflow",
            },
        )

        rc, emitted, _request = self.invoke_with_fake_run(
            ["--include-observability-summary", "--json"],
            payload,
        )

        self.assertEqual(rc, 0)
        summary = emitted["observability_summary"]
        self.assertEqual(summary["source"], "scheduler_tick")
        self.assertEqual(summary["status"], "no_eligible_issues")
        self.assertIsNone(summary["task_key"])
        self.assertEqual(summary["profile"]["validators"], [])
        self.assertEqual(summary["publication_mode"], "execution_only")

    def test_failed_payload_can_be_summarized(self) -> None:
        payload = self.scheduler_tick_payload(
            ok=False,
            status="automation_error",
            selected_task_key="AT-GH-901",
            safety={
                "scheduled_tick": True,
                "one_task_only": True,
                "dry_run": False,
                "confirmed": True,
                "github_mutated": False,
                "approved": False,
                "merged": False,
                "cleanup_performed": False,
                "branch_deleted": False,
                "worktree_deleted": False,
                "human_review_required": True,
            },
        )

        rc, emitted, _request = self.invoke_with_fake_run(
            ["--include-observability-summary", "--json"],
            payload,
        )

        self.assertEqual(rc, 1)
        self.assertFalse(emitted["ok"])
        summary = emitted["observability_summary"]
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["status"], "automation_error")
        self.assertEqual(summary["source"], "scheduler_tick")
        self.assertFalse(summary["safety"]["github_mutated"])
        self.assertFalse(summary["safety"]["branch_deleted"])
        self.assertFalse(summary["safety"]["worktree_deleted"])

    def test_use_execution_engine_defaults_off(self) -> None:
        payload = {
            "ok": True,
            "status": "dry_run",
            "mode": "dry_run",
            "safety": {"dry_run": True, "confirmed": False},
        }

        rc, _emitted, request = self.invoke_with_fake_run(["--json"], payload)

        self.assertEqual(rc, 0)
        self.assertFalse(request.use_execution_engine)

    def test_confirmed_use_execution_engine_flag_sets_request(self) -> None:
        payload = {
            "ok": True,
            "status": "execution_completed",
            "mode": "confirmed",
            "safety": {"dry_run": False, "confirmed": True},
        }

        rc, _emitted, request = self.invoke_with_fake_run(
            ["--confirmed", "--use-execution-engine", "--json"],
            payload,
        )

        self.assertEqual(rc, 0)
        self.assertTrue(request.confirmed)
        self.assertTrue(request.use_execution_engine)

    def test_dry_run_use_execution_engine_is_rejected(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = self.script.main(
                [*self.base_args(), "--use-execution-engine", "--json"]
            )

        emitted = json.loads(stdout.getvalue())
        self.assertEqual(rc, 1)
        self.assertFalse(emitted["ok"])
        self.assertEqual(emitted["status"], "error")
        joined_reasons = " ".join(emitted["reasons"])
        self.assertIn("use_execution_engine requires confirmed", joined_reasons)

    def test_observability_flags_expose_no_destructive_command_names(self) -> None:
        parser = self.script.build_parser()
        new_flags: list[str] = []
        for action in parser._actions:
            if action.dest in (
                "include_observability_summary",
                "observability_summary_only",
            ):
                new_flags.extend(action.option_strings)

        self.assertIn("--include-observability-summary", new_flags)
        self.assertIn("--observability-summary-only", new_flags)

        joined = " ".join(new_flags).lower()
        for token in (
            "approve",
            "merge",
            "cleanup",
            "archive",
            "closeout",
            "publish",
            "create-pr",
            "close-issue",
            "delete-branch",
            "delete-worktree",
            "branch-delete",
            "worktree-delete",
        ):
            self.assertNotIn(token, joined, token)


if __name__ == "__main__":
    unittest.main()
