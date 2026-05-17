"""Tests for scripts/run_issue_to_prepared_workspace_smoke.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_issue_to_prepared_workspace_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_issue_to_prepared_workspace_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunIssueToPreparedWorkspaceSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_help_flag_succeeds(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--skip-ingest-for-test", result.stdout)
        self.assertIn("--skip-prepare-for-test", result.stdout)

    def test_cli_smoke_succeeds_with_local_offline_issue_fixture(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-ISSUE-PREPARED-CLI",
                "--issue-number",
                "9101",
                "--workspace-root",
                str(self.workspace_root),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-ISSUE-PREPARED-CLI")
        self.assertEqual(payload["issue_number"], 9101)
        self.assertEqual(payload["ingestion_status"], "ingested")
        self.assertTrue(payload["ingestion_event_seen"])
        self.assertTrue(payload["issue_spec_artifact_seen"])
        self.assertTrue(payload["no_worktree_after_ingest"])
        self.assertEqual(payload["prepare_status"], "prepared")
        self.assertEqual(payload["dispatcher_status"], "waiting_approval")
        self.assertEqual(payload["final_status"], "waiting_approval")
        self.assertTrue(payload["review_evidence_available"])
        self.assertTrue(Path(payload["issue_spec_path"]).is_file())
        self.assertTrue(Path(payload["worktree_path"]).is_dir())
        self.assertTrue(payload["base_sha"])
        self.assertEqual(payload["validation_summary"]["status"], "passed")

        store = TaskMirrorStore(Path(payload["db_path"]))
        task = store.get_task("AT-ISSUE-PREPARED-CLI")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertIsNotNone(store.get_task_worktree("AT-ISSUE-PREPARED-CLI"))
        self.assertEqual(len(store.list_executor_runs("AT-ISSUE-PREPARED-CLI")), 1)
        self.assertEqual(len(store.list_validation_results("AT-ISSUE-PREPARED-CLI")), 1)

    def test_smoke_proves_ingestion_before_workspace_preparation(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-INGEST-BEFORE-PREPARE",
            issue_number=9102,
        )

        self.assertTrue(summary["ingestion_verified_before_prepare"])
        self.assertTrue(summary["ingestion_event_seen"])
        self.assertTrue(summary["issue_spec_artifact_seen"])
        self.assertEqual(summary["prepare_status"], "prepared")

    def test_smoke_proves_workspace_preparation_before_dispatch(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PREPARE-BEFORE-DISPATCH",
            issue_number=9103,
        )

        self.assertTrue(summary["prepare_verified_before_dispatch"])
        self.assertTrue(summary["base_sha"])
        self.assertEqual(summary["dispatcher_status"], "waiting_approval")

    def test_smoke_verifies_no_worktree_immediately_after_ingestion(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-NO-WORKTREE-AFTER-INGEST",
            issue_number=9104,
        )

        self.assertTrue(summary["no_worktree_after_ingest"])

    def test_smoke_reaches_waiting_approval_and_outputs_required_paths(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-ISSUE-PREPARED-OUTPUT",
            issue_number=9105,
        )

        self.assertEqual(summary["final_status"], "waiting_approval")
        self.assertTrue(summary["issue_spec_path"])
        self.assertTrue(summary["base_sha"])
        self.assertTrue(summary["worktree_path"])

    def test_cli_fails_clearly_when_ingestion_is_skipped(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-SKIP-INGEST",
                "--workspace-root",
                str(self.workspace_root),
                "--skip-ingest-for-test",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("issue ingestion must create task before workspace preparation", result.stderr)

    def test_cli_fails_clearly_when_prepare_is_skipped(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-SKIP-PREPARE",
                "--workspace-root",
                str(self.workspace_root),
                "--skip-prepare-for-test",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prepare workspace must record base_sha before dispatch", result.stderr)

    def test_smoke_does_not_create_prs_or_call_github(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("gh issue view", text)
        self.assertNotIn("gh issue edit", text)
        self.assertNotIn("gh pr create", text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("api.github", text)
        self.assertNotIn("/pulls", text)
        self.assertNotIn("pull_request", text)

    def test_smoke_does_not_run_forbidden_git_operations_or_cleanup(self) -> None:
        smoke = _load_smoke_module()
        original_run = smoke.subprocess.run
        calls: list[list[str]] = []

        def recording_run(args, *positional_args, **keyword_args):
            if isinstance(args, list):
                calls.append([str(item) for item in args])
            return original_run(args, *positional_args, **keyword_args)

        smoke.subprocess.run = recording_run
        try:
            summary = smoke.run_smoke(
                workspace_root=self.workspace_root,
                task_key="AT-ISSUE-PREPARED-GIT-SAFETY",
                issue_number=9106,
            )
        finally:
            smoke.subprocess.run = original_run

        self.assertEqual(summary["final_status"], "waiting_approval")
        git_calls = [call for call in calls if call and call[0] == "git"]
        forbidden = [
            ("push",),
            ("merge",),
            ("rebase",),
            ("worktree", "remove"),
            ("branch", "-d"),
            ("branch", "-D"),
        ]
        for call in git_calls:
            for pattern in forbidden:
                self.assertNotEqual(tuple(call[1 : 1 + len(pattern)]), pattern, call)

    def test_smoke_does_not_use_real_ai_executors(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("opencode", text)
        self.assertNotIn("codex", text)
        self.assertNotIn("claude", text)
        self.assertNotIn("pi_executor", text)
        self.assertNotIn('"pi"', text)


if __name__ == "__main__":
    unittest.main()
