from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "discover_github_issues.py"
MODULE = REPO_ROOT / "agent_taskflow" / "github_issue_discovery.py"


class DiscoverGitHubIssuesScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.issues_json_path = self.root / "issues.json"
        self.write_issues(
            [
                {
                    "number": 201,
                    "title": "CLI discover issue",
                    "state": "OPEN",
                    "labels": [{"name": "ready"}],
                    "url": "https://github.com/anderson930420/agent-taskflow/issues/201",
                    "createdAt": "2026-05-01T00:00:00Z",
                    "updatedAt": "2026-05-02T00:00:00Z",
                }
            ]
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_issues(self, issues: list[dict[str, object]]) -> None:
        self.issues_json_path.write_text(json.dumps(issues), encoding="utf-8")

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
                str(self.issues_json_path),
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

    def add_ingested_task(self, *, issue_number: int, task_key: str | None = None) -> None:
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        key = task_key or f"AT-GH-{issue_number}"
        store.upsert_task(
            TaskRecord(
                task_key=key,
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
                title=f"Existing {issue_number}",
            )
        )
        store.record_task_event(
            key,
            "github_issue_ingested",
            "github",
            message="GitHub issue ingested",
            payload={
                "kind": "github_issue_ingested",
                "repo": "anderson930420/agent-taskflow",
                "issue_number": issue_number,
            },
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
        self.assertIn("--repo", result.stdout)
        self.assertIn("--issues-json-path", result.stdout)
        self.assertIn("Discover", result.stdout)

    def test_script_prints_valid_json(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "discovered")
        self.assertEqual(payload["repo"], "anderson930420/agent-taskflow")
        self.assertEqual(payload["new_issues"][0]["number"], 201)
        self.assertEqual(payload["recommended_candidates"][0]["number"], 201)
        self.assertTrue(payload["safety"]["read_only"])

    def test_script_does_not_write_to_missing_db(self) -> None:
        self.assertFalse(self.db_path.exists())

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.db_path.exists())
        payload = json.loads(result.stdout)
        self.assertFalse(payload["safety"]["db_written"])

    def test_script_handles_empty_issue_list(self) -> None:
        self.write_issues([])

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["new_issues"], [])
        self.assertEqual(payload["recommended_candidates"], [])
        self.assertEqual(
            payload["summary"],
            {
                "new_issue_count": 0,
                "already_ingested_count": 0,
                "closed_or_blocked_count": 0,
                "not_eligible_count": 0,
                "recommended_candidate_count": 0,
            },
        )

    def test_script_handles_already_ingested_local_tasks_without_writing_more(self) -> None:
        self.add_ingested_task(issue_number=201, task_key="CUSTOM-201")
        before_size = self.db_path.stat().st_size
        store = TaskMirrorStore(self.db_path)
        before_events = len(store.list_task_events("CUSTOM-201"))

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["new_issues"], [])
        self.assertEqual(payload["recommended_candidates"], [])
        self.assertEqual(payload["already_ingested"][0]["task_key"], "CUSTOM-201")
        self.assertEqual(self.db_path.stat().st_size, before_size)
        self.assertEqual(len(store.list_task_events("CUSTOM-201")), before_events)
        self.assertEqual(store.list_task_artifacts("CUSTOM-201"), [])
        self.assertIsNone(store.get_task_worktree("CUSTOM-201"))

    def test_script_and_module_do_not_call_intake_or_mutation_helpers(self) -> None:
        text = (SCRIPT.read_text(encoding="utf-8") + "\n" + MODULE.read_text(encoding="utf-8")).lower()

        forbidden = [
            "ingest_github_issue(",
            "dispatch_task(",
            "prepare_task_workspace",
            "push_task_branch(",
            "create_draft_pr(",
            "create_pr_handoff(",
            "merge_pull_request",
            "record_approval_decision(",
            "git push",
            "gh pr create",
            "gh pr merge",
            "git worktree remove",
            "delete_worktree",
            "remove_worktree",
            "cleanup(",
        ]
        for item in forbidden:
            self.assertNotIn(item, text)


if __name__ == "__main__":
    unittest.main()
