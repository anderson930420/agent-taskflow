"""Tests for scripts/run_draft_pr_fake_gh_golden_path_smoke.py."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_draft_pr_fake_gh_golden_path_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_draft_pr_fake_gh_golden_path_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunDraftPrFakeGhGoldenPathSmokeTests(unittest.TestCase):
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
                "AT-DRAFT-FAKE-CLI",
                "--issue-number",
                "9501",
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
        self.assertIn("--skip-draft-pr-for-test", result.stdout)
        self.assertIn("--fake-view-non-draft-for-test", result.stdout)

    def test_cli_smoke_succeeds_locally_and_emits_json(self) -> None:
        result = self._run_cli()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-DRAFT-FAKE-CLI")
        self.assertEqual(payload["final_status"], "waiting_approval")
        self.assertTrue(payload["review_evidence_available"])
        self.assertTrue(payload["draft_pr_artifact_seen"])
        self.assertTrue(payload["draft_pr_event_seen"])

    def test_smoke_reaches_waiting_approval_before_draft_pr_creation(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-READY",
            issue_number=9502,
        )

        self.assertEqual(summary["final_status"], "waiting_approval")
        self.assertTrue(summary["fake_create_command_seen"])
        self.assertTrue(summary["fake_view_command_seen"])

    def test_smoke_creates_draft_pr_json(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-FILE",
            issue_number=9503,
        )

        draft_path = Path(summary["draft_pr_json_path"])
        self.assertTrue(draft_path.is_file())
        payload = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "draft_pr_created")
        self.assertEqual(payload["artifact_type"], "draft_pr")
        self.assertTrue(payload["is_draft"])

    def test_smoke_records_draft_pr_artifact_and_event(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-STORE",
            issue_number=9504,
        )

        self.assertTrue(summary["draft_pr_artifact_seen"])
        self.assertTrue(summary["draft_pr_event_seen"])
        store = TaskMirrorStore(Path(summary["db_path"]))
        artifacts = store.list_task_artifacts("AT-DRAFT-FAKE-STORE")
        events = store.list_task_events("AT-DRAFT-FAKE-STORE")
        self.assertTrue(any(a.artifact_type == "draft_pr" for a in artifacts))
        self.assertTrue(any(e.event_type == "draft_pr_created" for e in events))

    def test_smoke_verifies_fake_create_command_contract(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-CREATE",
            issue_number=9505,
        )

        command = summary["gh_create_command"]
        self.assertEqual(command[:3], ["gh", "pr", "create"])
        self.assertIn("--draft", command)
        self.assertIn("--repo", command)
        self.assertIn("--base", command)
        self.assertIn("--head", command)
        self.assertIn("--title", command)
        self.assertIn("--body", command)
        self.assertNotIn("--json", command)

    def test_smoke_verifies_fake_view_command_contract(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-VIEW",
            issue_number=9506,
        )

        command = summary["gh_view_command"]
        self.assertEqual(command[:4], ["gh", "pr", "view", summary["fake_pr_url"]])
        self.assertIn("--repo", command)
        self.assertIn("--json", command)
        self.assertEqual(
            command[command.index("--json") + 1],
            "url,number,headRefName,baseRefName,isDraft",
        )

    def test_smoke_verifies_original_handoff_json_remains_unchanged(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-HANDOFF",
            issue_number=9507,
        )

        self.assertTrue(summary["handoff_unchanged"])
        handoff = json.loads(
            Path(summary["pr_handoff_json_path"]).read_text(encoding="utf-8")
        )
        self.assertFalse(handoff["safety"]["pr_created"])
        self.assertFalse(handoff["safety"]["github_mutated"])

    def test_smoke_verifies_safety_booleans(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-SAFETY",
            issue_number=9508,
        )

        safety = summary["safety"]
        self.assertTrue(safety["pr_created"])
        self.assertFalse(safety["pushed"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertTrue(safety["human_review_required"])
        handoff_safety = summary["handoff_safety"]
        self.assertFalse(handoff_safety["pr_created"])
        self.assertFalse(handoff_safety["github_mutated"])

    def test_smoke_output_includes_required_fields(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-DRAFT-FAKE-OUTPUT",
            issue_number=9509,
        )

        self.assertTrue(summary["draft_pr_json_path"])
        self.assertEqual(
            summary["fake_pr_url"],
            "https://github.com/anderson930420/agent-taskflow/pull/9999",
        )
        self.assertEqual(summary["fake_pr_number"], 9999)
        self.assertTrue(summary["gh_create_command"])
        self.assertTrue(summary["gh_view_command"])

    def test_cli_fails_clearly_when_draft_pr_is_skipped(self) -> None:
        result = self._run_cli("--skip-draft-pr-for-test")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Draft PR creation was skipped after PR handoff", result.stderr)

    def test_cli_fails_clearly_when_fake_view_reports_non_draft(self) -> None:
        result = self._run_cli("--fake-view-non-draft-for-test")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not confirm a draft PR", result.stderr)

    def test_cli_fails_clearly_when_fake_create_omits_url(self) -> None:
        result = self._run_cli("--fake-create-missing-url-for-test")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not print a created PR URL", result.stderr)

    def test_static_safety_no_real_gh_or_forbidden_operations(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        lowered = text.lower()

        self.assertNotIn("subprocess.run", text)
        self.assertNotIn("shell=True", text)
        self.assertNotIn("shell = True", text)
        forbidden = [
            "git push",
            "gh pr merge",
            "gh pr review --approve",
            "gh issue edit",
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
            self.assertNotIn(phrase, lowered)

    def test_smoke_does_not_use_real_ai_executors(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("opencode", text)
        self.assertNotIn("codex", text)
        self.assertNotIn("claude", text)
        self.assertNotIn("pi_executor", text)
        self.assertNotIn('"pi"', text)


if __name__ == "__main__":
    unittest.main()
