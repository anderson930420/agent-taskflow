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
SCRIPT = REPO_ROOT / "scripts" / "intake_github_issues.py"


class IntakeGitHubIssuesScriptTests(unittest.TestCase):
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
                        "title": "CLI intake issue",
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
                "--repo-path",
                str(self.repo),
                "--artifact-root",
                str(self.root / "artifacts"),
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(self.issue_json_path),
                "--issue",
                "201",
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
        self.assertIn("--confirm-intake", result.stdout)
        self.assertIn("--issue", result.stdout)
        self.assertIn("--repo-path", result.stdout)
        self.assertIn("--artifact-root", result.stdout)

    def test_script_defaults_to_dry_run_without_confirm_intake(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertFalse(payload["written"])
        self.assertEqual(payload["selected"][0]["action"], "would_ingest")
        self.assertFalse(self.db_path.exists())
        self.assertEqual(
            sorted(payload.keys()),
            [
                "artifact_root",
                "board",
                "db_path",
                "mode",
                "ok",
                "project",
                "repo",
                "repo_path",
                "safety",
                "selected",
                "selected_issue_numbers",
                "summary",
                "written",
            ],
        )

    def test_script_confirmed_intake_writes_queued_task_and_event(self) -> None:
        result = self.run_script("--confirm-intake")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "confirmed")
        self.assertTrue(payload["written"])
        self.assertEqual(payload["selected"][0]["action"], "ingested")

        store = TaskMirrorStore(self.db_path)
        task = store.get_task("GH-201")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertIsNone(store.get_task_worktree("GH-201"))
        events = store.list_task_events("GH-201")
        self.assertEqual(len(events), 1)
        event_payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(event_payload["task_key"], "GH-201")
        self.assertEqual(event_payload["status"], "queued")

    def test_script_fails_when_selected_issue_is_missing_from_fixture_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--artifact-root",
                str(self.root / "artifacts"),
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(self.issue_json_path),
                "--issue",
                "999",
                "--confirm-intake",
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
        self.assertEqual(payload["selected"][0]["action"], "failed")
        self.assertEqual(payload["selected"][0]["issue_number"], 999)
        self.assertFalse(payload["written"])
        self.assertEqual(payload["summary"]["failed_count"], 1)

    def test_script_fails_on_invalid_fetch_input(self) -> None:
        bad_json = self.root / "bad-issues.json"
        bad_json.write_text("{not-json", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--artifact-root",
                str(self.root / "artifacts"),
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(bad_json),
                "--issue",
                "201",
                "--confirm-intake",
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
        self.assertEqual(payload["selected"], [])
        self.assertFalse(payload["written"])
        self.assertIn("invalid issues JSON", payload["summary"])

    def test_script_is_idempotent_for_already_ingested_issue(self) -> None:
        first = self.run_script("--confirm-intake")
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_script("--confirm-intake")
        self.assertEqual(second.returncode, 0, second.stderr)
        payload = json.loads(second.stdout)
        self.assertEqual(payload["selected"][0]["action"], "already_ingested")
        self.assertFalse(payload["written"])

        store = TaskMirrorStore(self.db_path)
        self.assertEqual(len(store.list_tasks()), 1)
        self.assertEqual(len(store.list_task_events("GH-201")), 1)

    def test_script_rejects_non_absolute_repo_or_artifact_paths(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                "relative/repo",
                "--artifact-root",
                str(self.root / "artifacts"),
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(self.issue_json_path),
                "--issue",
                "201",
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
        self.assertIn("repo_path must be absolute", payload["summary"])

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--artifact-root",
                "relative/artifacts",
                "--db-path",
                str(self.db_path),
                "--issues-json-path",
                str(self.issue_json_path),
                "--issue",
                "201",
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
        self.assertIn("artifact_root must be absolute", payload["summary"])

    def test_script_output_has_stable_shape(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            sorted(payload.keys()),
            [
                "artifact_root",
                "board",
                "db_path",
                "mode",
                "ok",
                "project",
                "repo",
                "repo_path",
                "safety",
                "selected",
                "selected_issue_numbers",
                "summary",
                "written",
            ],
        )
        self.assertEqual(
            sorted(payload["selected"][0].keys()),
            ["action", "issue_number", "issue_url", "status", "task_key", "title"],
        )

    def test_script_does_not_create_task_worktrees_or_call_mutation_helpers(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("task_worktrees", text)
        self.assertNotIn("prepare_task_workspace", text)
        self.assertNotIn("dispatch_task(", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("gh pr create", text)
        self.assertNotIn("gh pr merge", text)


if __name__ == "__main__":
    unittest.main()
