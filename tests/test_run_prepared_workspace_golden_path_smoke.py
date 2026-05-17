"""Tests for scripts/run_prepared_workspace_golden_path_smoke.py.

These tests are local-only. They use a temporary git repository, TestClient,
and script-local executor and validator implementations.
"""

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
SCRIPT = REPO_ROOT / "scripts" / "run_prepared_workspace_golden_path_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_prepared_workspace_golden_path_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunPreparedWorkspaceGoldenPathSmokeTests(unittest.TestCase):
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
        self.assertIn("--workspace-root", result.stdout)
        self.assertIn("--skip-prepare-for-test", result.stdout)

    def test_cli_smoke_succeeds_with_default_fake_local_path(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-PREPARED-WORKSPACE-TEST",
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
        self.assertEqual(payload["task_key"], "AT-PREPARED-WORKSPACE-TEST")
        self.assertEqual(payload["prepare_status"], "prepared")
        self.assertEqual(payload["dispatcher_status"], "waiting_approval")
        self.assertEqual(payload["final_status"], "waiting_approval")
        self.assertTrue(payload["review_evidence_available"])
        self.assertTrue(payload["prepare_verified_before_dispatch"])
        self.assertIn("base_sha", payload)
        self.assertTrue(payload["base_sha"])
        self.assertIn("worktree_path", payload)
        self.assertTrue(Path(payload["worktree_path"]).is_dir())
        self.assertEqual(payload["validation_summary"]["status"], "passed")
        self.assertIn("mission_contract.json", payload["readbacks"]["artifacts"])
        self.assertIn(
            "prepared_workspace_smoke_result.txt",
            payload["readbacks"]["artifacts"],
        )

        store = TaskMirrorStore(Path(payload["db_path"]))
        task = store.get_task("AT-PREPARED-WORKSPACE-TEST")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        worktree = store.get_task_worktree("AT-PREPARED-WORKSPACE-TEST")
        self.assertIsNotNone(worktree)
        assert worktree is not None
        self.assertEqual(worktree.status, "active")
        self.assertEqual(worktree.base_sha, payload["base_sha"])
        self.assertEqual(str(worktree.worktree_path), payload["worktree_path"])
        self.assertEqual(len(store.list_executor_runs("AT-PREPARED-WORKSPACE-TEST")), 1)
        self.assertEqual(len(store.list_validation_results("AT-PREPARED-WORKSPACE-TEST")), 1)

    def test_smoke_verifies_prepare_workspace_happened_before_dispatch(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PREPARED-BEFORE-DISPATCH",
        )

        self.assertTrue(summary["prepare_verified_before_dispatch"])
        self.assertEqual(summary["prepare_status"], "prepared")
        self.assertEqual(summary["dispatcher_status"], "waiting_approval")
        store = TaskMirrorStore(Path(summary["db_path"]))
        worktree = store.get_task_worktree("AT-PREPARED-BEFORE-DISPATCH")
        self.assertIsNotNone(worktree)
        assert worktree is not None
        self.assertIsNotNone(worktree.base_sha)

    def test_cli_fails_clearly_when_prepare_step_is_skipped(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-PREPARED-SKIPPED",
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

        self.assertNotIn("gh pr create", text)
        self.assertNotIn("github.com", text)
        self.assertNotIn("api.github", text)
        self.assertNotIn("/pulls", text)
        self.assertNotIn("pull_request", text)

    def test_smoke_does_not_run_forbidden_git_operations(self) -> None:
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
                task_key="AT-PREPARED-GIT-SAFETY",
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

    def test_smoke_output_includes_base_sha_worktree_and_waiting_approval(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PREPARED-OUTPUT",
        )

        self.assertTrue(summary["base_sha"])
        self.assertTrue(summary["worktree_path"])
        self.assertEqual(summary["final_status"], "waiting_approval")
        self.assertEqual(summary["dispatcher_status"], "waiting_approval")


if __name__ == "__main__":
    unittest.main()
