"""Tests for scripts/run_pr_handoff_golden_path_smoke.py."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_pr_handoff_golden_path_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_pr_handoff_golden_path_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunPrHandoffGoldenPathSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-PR-HANDOFF-CLI",
                "--issue-number",
                "9301",
                "--workspace-root",
                str(self.workspace_root),
                *extra,
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_help_succeeds(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--skip-handoff-for-test", result.stdout)

    def test_cli_smoke_succeeds_locally_and_emits_json(self) -> None:
        result = self._run_cli()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-PR-HANDOFF-CLI")
        self.assertEqual(payload["final_status"], "waiting_approval")
        self.assertEqual(payload["handoff_status"], "created")
        self.assertTrue(payload["review_evidence_available"])

    def test_smoke_reaches_waiting_approval_before_handoff(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PR-HANDOFF-READY",
            issue_number=9302,
        )

        self.assertEqual(summary["final_status"], "waiting_approval")
        self.assertEqual(summary["handoff_status"], "created")

    def test_smoke_creates_handoff_json_and_markdown(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PR-HANDOFF-FILES",
            issue_number=9303,
        )

        json_path = Path(summary["pr_handoff_json_path"])
        markdown_path = Path(summary["pr_handoff_markdown_path"])
        self.assertTrue(json_path.is_file())
        self.assertTrue(markdown_path.is_file())
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifact_type"], "pr_handoff")
        self.assertEqual(payload["task_status"], "waiting_approval")
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("Task Summary", markdown)
        self.assertIn("Proposed PR", markdown)
        self.assertIn("This package did not create a PR.", markdown)

    def test_smoke_records_handoff_artifact_and_event(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PR-HANDOFF-STORE",
            issue_number=9304,
        )

        self.assertTrue(summary["pr_handoff_artifact_seen"])
        self.assertTrue(summary["pr_handoff_event_seen"])
        store = TaskMirrorStore(Path(summary["db_path"]))
        artifacts = store.list_task_artifacts("AT-PR-HANDOFF-STORE")
        events = store.list_task_events("AT-PR-HANDOFF-STORE")
        self.assertTrue(any(a.artifact_type == "pr_handoff" for a in artifacts))
        self.assertTrue(any(e.event_type == "pr_handoff_created" for e in events))

    def test_smoke_verifies_conservative_safety_booleans(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PR-HANDOFF-SAFETY",
            issue_number=9305,
        )

        safety = summary["safety"]
        self.assertFalse(safety["pr_created"])
        self.assertFalse(safety["pushed"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["github_mutated"])
        self.assertTrue(safety["human_review_required"])

    def test_smoke_create_command_preview_is_inert_text_only(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PR-HANDOFF-INERT",
            issue_number=9306,
        )

        preview = summary["create_command_preview"]
        self.assertIsInstance(preview, str)
        self.assertIn("gh pr create", preview)
        self.assertTrue(summary["proposed_pr_draft_recommended"])

    def test_smoke_output_includes_required_handoff_fields(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PR-HANDOFF-OUTPUT",
            issue_number=9307,
        )

        self.assertTrue(summary["base_sha"])
        self.assertTrue(summary["head_sha"])
        self.assertTrue(summary["changed_files"])
        self.assertEqual(summary["final_status"], "waiting_approval")
        self.assertTrue(summary["pr_handoff_json_path"])
        self.assertTrue(summary["pr_handoff_markdown_path"])
        self.assertEqual(summary["proposed_pr_base_branch"], "main")
        self.assertEqual(summary["proposed_pr_head_branch"], summary["branch"])

    def test_cli_fails_clearly_when_handoff_is_skipped(self) -> None:
        result = self._run_cli("--skip-handoff-for-test")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PR handoff was skipped after waiting_approval", result.stderr)

    def test_smoke_does_not_call_github_or_create_prs(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("gh issue view", text)
        self.assertNotIn("gh issue edit", text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("api.github", text)
        self.assertNotIn("/pulls", text)
        self.assertNotIn("pull_request", text)
        self.assertNotIn("subprocess.run", text)

    def test_smoke_does_not_run_forbidden_git_or_cleanup_operations(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()
        forbidden = [
            "git push",
            "git merge",
            "git rebase",
            "git branch -d",
            "git branch -D",
            "git worktree remove",
            "git reset --hard",
            "force push",
            "delete_branch",
            "delete_worktree",
            "cleanup automation",
        ]

        for phrase in forbidden:
            self.assertNotIn(phrase, text)

    def test_smoke_does_not_use_real_ai_executors(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("opencode", text)
        self.assertNotIn("codex", text)
        self.assertNotIn("claude", text)
        self.assertNotIn("pi_executor", text)
        self.assertNotIn('"pi"', text)

    def test_smoke_does_not_use_shell_true(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("shell=True", text)
        self.assertNotIn("shell = True", text)


if __name__ == "__main__":
    unittest.main()
