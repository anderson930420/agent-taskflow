from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ingest_github_issue.py"


class IngestGitHubIssueScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.issue_json_path = self.root / "issue.json"
        self.issue_json_path.write_text(
            json.dumps(
                {
                    "number": 101,
                    "title": "CLI ingest issue",
                    "body": "CLI issue body",
                    "state": "OPEN",
                    "labels": [{"name": "cli"}],
                    "author": {"login": "octocat"},
                    "url": "https://github.com/anderson930420/agent-taskflow/issues/101",
                    "createdAt": "2026-05-01T00:00:00Z",
                    "updatedAt": "2026-05-02T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--issue-number",
                "101",
                "--db-path",
                str(self.db_path),
                "--local-repo-path",
                str(self.local_repo),
                "--artifact-root",
                str(self.root / "artifacts"),
                "--issue-json-path",
                str(self.issue_json_path),
                *extra_args,
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_help_flag_succeeds(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--repo", result.stdout)
        self.assertIn("--issue-number", result.stdout)
        self.assertIn("--issue-json-path", result.stdout)

    def test_cli_success_with_fake_issue_source(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ingested")
        self.assertEqual(payload["task_key"], "AT-GH-101")
        self.assertEqual(payload["issue_state"], "open")
        self.assertTrue(payload["wrote_task"])
        self.assertTrue(payload["wrote_artifact"])
        self.assertTrue(payload["recorded_event"])

        store = TaskMirrorStore(self.db_path)
        task = store.get_task("AT-GH-101")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertIsNone(store.get_task_worktree("AT-GH-101"))
        self.assertEqual(len(store.list_task_artifacts("AT-GH-101")), 1)
        self.assertEqual(len(store.list_task_events("AT-GH-101")), 1)
        self.assertTrue(Path(payload["issue_spec_path"]).is_file())

    def test_cli_dry_run_success_writes_nothing(self) -> None:
        result = self.run_script("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["wrote_task"])
        self.assertFalse(payload["wrote_artifact"])
        self.assertFalse(payload["recorded_event"])
        self.assertFalse(self.db_path.exists())
        self.assertFalse(Path(payload["issue_spec_path"]).exists())

    def test_cli_missing_issue_fetch_failure_exits_nonzero(self) -> None:
        self.issue_json_path.write_text(
            json.dumps({"number": 999, "title": "Wrong issue", "state": "OPEN"}),
            encoding="utf-8",
        )

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("does not match requested", payload["summary"])

    def test_cli_custom_task_key_is_used(self) -> None:
        result = self.run_script("--task-key", "CUSTOM-101")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], "CUSTOM-101")
        store = TaskMirrorStore(self.db_path)
        self.assertIsNotNone(store.get_task("CUSTOM-101"))

    def test_script_contains_no_forbidden_write_or_cleanup_operations(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("gh issue edit", text)
        self.assertNotIn("gh pr create", text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("git merge", text)
        self.assertNotIn("git rebase", text)
        self.assertNotIn("git branch -d", text)
        self.assertNotIn("git branch -d", text)
        self.assertNotIn("git worktree remove", text)
        self.assertNotIn("dispatch_task", text)
        self.assertNotIn("prepare_task_workspace", text)


if __name__ == "__main__":
    unittest.main()
