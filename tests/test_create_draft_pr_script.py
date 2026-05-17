"""Tests for scripts/create_draft_pr.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_draft_pr.py"
MODULE = REPO_ROOT / "agent_taskflow" / "draft_pr.py"


class CreateDraftPrScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.repo / ".worktrees" / "AT-DRAFT-CLI"
        self.db_path = self.root / "state.db"
        self.artifact_dir = self.root / "artifacts" / "AT-DRAFT-CLI"
        self.handoff_dir = self.root / "artifacts" / "pr_handoff" / "AT-DRAFT-CLI"
        self.handoff_json = self.handoff_dir / "pr_handoff.json"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._create_fixture()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _create_fixture(self) -> None:
        self.repo.mkdir(parents=True)
        self.worktree.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)
        self.handoff_dir.mkdir(parents=True)
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-DRAFT-CLI",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Draft PR CLI test",
                status="waiting_approval",
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-DRAFT-CLI",
                repo_path=self.repo,
                worktree_path=self.worktree,
                branch="task/AT-DRAFT-CLI",
                base_branch="main",
                base_sha="abc123",
                status="active",
            )
        )
        payload = {
            "schema_version": "1",
            "artifact_type": "pr_handoff",
            "task_key": "AT-DRAFT-CLI",
            "task_status": "waiting_approval",
            "repo": "anderson930420/agent-taskflow",
            "proposed_pr": {
                "title": "AT-DRAFT-CLI: Draft PR CLI test",
                "body": "Task: AT-DRAFT-CLI\n",
                "base_branch": "main",
                "head_branch": "task/AT-DRAFT-CLI",
                "draft_recommended": True,
            },
            "safety": {
                "pr_created": False,
                "pushed": False,
                "merged": False,
                "cleanup_performed": False,
                "github_mutated": False,
                "human_review_required": True,
            },
        }
        self.handoff_json.write_text(json.dumps(payload), encoding="utf-8")
        self.store.record_task_artifact("AT-DRAFT-CLI", "pr_handoff", self.handoff_json)

    def test_help_succeeds(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--confirm-create-pr", result.stdout)

    def test_cli_dry_run_succeeds_by_default(self) -> None:
        result = self._run(
            "--task-key",
            "AT-DRAFT-CLI",
            "--db-path",
            str(self.db_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["github_mutated"])
        self.assertFalse(payload["pr_created"])
        self.assertIn("gh pr create --draft", payload["command_preview"])

    def test_cli_explicit_dry_run_wins_over_confirm(self) -> None:
        result = self._run(
            "--task-key",
            "AT-DRAFT-CLI",
            "--db-path",
            str(self.db_path),
            "--confirm-create-pr",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["github_mutated"])

    def test_cli_failure_exits_nonzero_with_clear_json(self) -> None:
        result = self._run(
            "--task-key",
            "AT-MISSING",
            "--db-path",
            str(self.db_path),
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("Task not found", payload["summary"])

    def test_static_safety_forbidden_commands_are_absent(self) -> None:
        text = MODULE.read_text(encoding="utf-8") + "\n" + SCRIPT.read_text(encoding="utf-8")
        forbidden = [
            "git push",
            "gh repo sync",
            "git merge",
            "gh pr merge",
            "gh pr review --approve",
            "gh issue edit",
            "git rebase",
            "git branch -d",
            "git branch -D",
            "git worktree remove",
            "git reset --hard",
            "delete_branch",
            "delete_worktree",
            "shell=True",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)

    def test_static_no_background_webhook_or_frontend_surface(self) -> None:
        text = MODULE.read_text(encoding="utf-8") + "\n" + SCRIPT.read_text(encoding="utf-8")
        forbidden = [
            "webhook",
            "polling",
            "threading",
            "asyncio",
            "mission-control",
            "npm",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
