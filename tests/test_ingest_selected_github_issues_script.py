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
SCRIPT = REPO_ROOT / "scripts" / "ingest_selected_github_issues.py"


class IngestSelectedGitHubIssuesScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.issue_json_path = self.root / "issues.json"
        self.issue_json_path.write_text(
            json.dumps(
                [
                    {
                        "number": 201,
                        "title": "CLI selected intake issue",
                        "body": "CLI issue body",
                        "state": "OPEN",
                        "labels": [{"name": "ready"}],
                        "author": {"login": "octocat"},
                        "url": "https://github.com/anderson930420/agent-taskflow/issues/201",
                        "createdAt": "2026-05-01T00:00:00Z",
                        "updatedAt": "2026-05-02T00:00:00Z",
                    }
                ]
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
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
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
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--issue-number", result.stdout)
        self.assertIn("--issues", result.stdout)
        self.assertIn("--issues-json-path", result.stdout)

    def test_script_requires_explicit_issue_selection(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(self.issue_json_path),
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("issue_numbers must not be empty", payload["summary"])

    def test_script_prints_valid_json(self) -> None:
        result = self.run_script("--issue-number", "201")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["selected_issue_numbers"], [201])
        self.assertEqual(payload["ingested"][0]["issue_number"], 201)
        self.assertTrue(payload["safety"]["db_written"])

    def test_script_writes_selected_issue_into_task_mirror_as_queued(self) -> None:
        result = self.run_script("--issue-number", "201")

        self.assertEqual(result.returncode, 0, result.stderr)
        store = TaskMirrorStore(self.db_path)
        task = store.get_task("AT-GH-201")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

    def test_script_writes_issue_spec_artifact(self) -> None:
        result = self.run_script("--issue-number", "201")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        artifact_path = Path(payload["ingested"][0]["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        self.assertIn("CLI selected intake issue", artifact_path.read_text(encoding="utf-8"))

    def test_script_records_github_issue_ingested_event(self) -> None:
        result = self.run_script("--issue-number", "201")

        self.assertEqual(result.returncode, 0, result.stderr)
        store = TaskMirrorStore(self.db_path)
        events = store.list_task_events("AT-GH-201")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0].payload_json or "{}")
        self.assertTrue(payload["selected_intake"])

    def test_script_does_not_prepare_worktree_or_dispatch_or_push_or_pr_or_cleanup(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("prepare_worktree", text)
        self.assertNotIn("dispatch_task(", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("gh pr create", text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("git worktree remove", text)
        self.assertNotIn("prepare_task_workspace", text)

    def test_script_handles_duplicate_selected_issue_safely(self) -> None:
        result = self.run_script("--issue-number", "201", "--issue-number", "201")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["selected_issue_numbers"], [201])
        store = TaskMirrorStore(self.db_path)
        self.assertEqual(len(store.list_task_events("AT-GH-201")), 1)
        self.assertEqual(len(store.list_task_artifacts("AT-GH-201")), 1)

    def test_script_handles_empty_selected_issue_list_as_argument_error(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(self.issue_json_path),
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])

    def test_script_handles_not_found_selected_issue_as_failed_but_process_completes(self) -> None:
        result = self.run_script("--issue-number", "999", "--issue-number", "201")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["failed"][0]["issue_number"], 999)
        self.assertEqual(payload["ingested"][0]["issue_number"], 201)


if __name__ == "__main__":
    unittest.main()
