from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import recommend_post_merge_cleanup as script


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeInspectorRunner:
    def __init__(
        self,
        *,
        pr_state: str = "MERGED",
        merged_at: str | None = "2026-05-18T00:00:00Z",
        merge_commit: dict[str, Any] | None = {"oid": "deadbeef"},
        status_stdout: str = "",
        remote_exists: bool = True,
        pr_returncode: int = 0,
    ) -> None:
        self.pr_state = pr_state
        self.merged_at = merged_at
        self.merge_commit = merge_commit
        self.status_stdout = status_stdout
        self.remote_exists = remote_exists
        self.pr_returncode = pr_returncode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if args[:3] == ["gh", "pr", "view"]:
            payload = {
                "number": 123,
                "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                "state": self.pr_state,
                "isDraft": False,
                "mergedAt": self.merged_at,
                "mergeCommit": self.merge_commit,
                "headRefName": "task/AT-POST-MERGE-CLI-001",
                "baseRefName": "main",
                "title": "Post merge cleanup task",
            }
            return FakeCompletedProcess(
                returncode=self.pr_returncode,
                stdout=json.dumps(payload),
            )
        if args[:2] == ["git", "status"]:
            return FakeCompletedProcess(returncode=0, stdout=self.status_stdout)
        if args[:2] == ["git", "branch"] and "--list" in args:
            return FakeCompletedProcess(returncode=0, stdout="task/AT-POST-MERGE-CLI-001\n")
        if args[:2] == ["git", "branch"] and "--merged" in args:
            return FakeCompletedProcess(returncode=0, stdout="task/AT-POST-MERGE-CLI-001\n")
        if args[:2] == ["git", "ls-remote"] and "--heads" in args:
            stdout = "deadbeef\trefs/heads/task/AT-POST-MERGE-CLI-001\n" if self.remote_exists else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)
        raise AssertionError(f"unexpected command: {args}")


class RecommendPostMergeCleanupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "worktree"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-POST-MERGE-CLI-001"
        self.branch = f"task/{self.task_key}"
        self._seed_task()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(self) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        self.worktree.mkdir(parents=True, exist_ok=True)
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Post merge cleanup task",
                status="waiting_approval",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self.task_key,
                repo_path=self.repo,
                worktree_path=self.worktree,
                branch=self.branch,
                base_branch="main",
                base_sha="base-sha",
                status="active",
            )
        )
        draft_pr_path = artifact_dir / "draft_pr.json"
        draft_pr_path.write_text(
            json.dumps(
                {
                    "kind": "draft_pr_created",
                    "artifact_type": "draft_pr",
                    "task_key": self.task_key,
                    "repo": "anderson930420/agent-taskflow",
                    "base_branch": "main",
                    "head_branch": self.branch,
                    "title": "Post merge cleanup task",
                    "body": "Draft PR body",
                    "draft": True,
                    "pr_number": 123,
                    "pr_url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                    "issue_closed": False,
                    "requires_human_confirmation": True,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(self.task_key, "draft_pr", draft_pr_path)
        self.store.record_task_event(
            self.task_key,
            "draft_pr_created",
            "draft_pr_confirm",
            payload={
                "kind": "draft_pr_created",
                "artifact_type": "draft_pr",
                "task_key": self.task_key,
                "repo": "anderson930420/agent-taskflow",
                "base_branch": "main",
                "head_branch": self.branch,
                "title": "Post merge cleanup task",
                "body": "Draft PR body",
                "draft": True,
                "pr_number": 123,
                "pr_url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "issue_closed": False,
                "requires_human_confirmation": True,
            },
        )

    def _run(self, argv: list[str], *, runner: FakeInspectorRunner | None = None) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = script.main(argv, runner=runner)
        return code, stdout.getvalue()

    def test_script_requires_task_key(self) -> None:
        with self.assertRaises(SystemExit):
            script.main([])

    def test_script_prints_valid_json(self) -> None:
        code, output = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=FakeInspectorRunner(),
        )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "merged_recommend_cleanup")

    def test_script_supports_pretty_output(self) -> None:
        code, output = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--pretty",
            ],
            runner=FakeInspectorRunner(),
        )

        self.assertEqual(code, 0)
        self.assertIn("\n  \"status\": ", output)

    def test_script_handles_missing_db_without_creating_file(self) -> None:
        missing_db = self.root / "missing" / "state.db"
        code, output = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(missing_db),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=FakeInspectorRunner(),
        )

        self.assertEqual(code, 1)
        self.assertFalse(missing_db.exists())
        payload = json.loads(output)
        self.assertEqual(payload["status"], "not_found")

    def test_script_handles_not_merged_pr_with_no_cleanup_recommendation(self) -> None:
        code, output = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=FakeInspectorRunner(pr_state="OPEN", merged_at=None, merge_commit=None),
        )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "not_merged")
        self.assertEqual(payload["recommended_cleanup"], [])

    def test_script_handles_merged_pr_with_cleanup_recommendation(self) -> None:
        code, output = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=FakeInspectorRunner(),
        )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["summary"]["cleanup_recommended"])
        self.assertGreaterEqual(len(payload["recommended_cleanup"]), 4)

    def test_script_does_not_update_task_status(self) -> None:
        code, _ = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=FakeInspectorRunner(),
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")

    def test_script_does_not_remove_worktree_or_delete_branches(self) -> None:
        runner = FakeInspectorRunner()
        code, _ = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=runner,
        )

        self.assertEqual(code, 0)
        self.assertTrue(self.worktree.exists())
        prefixes = [tuple(call["args"][:3]) for call in runner.calls]
        self.assertNotIn(("git", "worktree", "remove"), prefixes)
        self.assertNotIn(("git", "branch", "-d"), prefixes)
        self.assertNotIn(("git", "branch", "-D"), prefixes)
        self.assertNotIn(("git", "push", "--delete"), prefixes)

    def test_script_does_not_close_issue_merge_or_approve(self) -> None:
        runner = FakeInspectorRunner()
        code, _ = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=runner,
        )

        self.assertEqual(code, 0)
        prefixes = [tuple(call["args"][:3]) for call in runner.calls]
        forbidden = {
            ("gh", "pr", "merge"),
            ("gh", "pr", "review"),
            ("gh", "pr", "ready"),
            ("gh", "pr", "close"),
            ("gh", "issue", "close"),
            ("git", "merge", "--no-ff"),
        }
        for prefix in forbidden:
            self.assertNotIn(prefix, prefixes)

    def test_script_does_not_cleanup_artifacts_or_mutate_github(self) -> None:
        runner = FakeInspectorRunner()
        code, _ = self._run(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
            ],
            runner=runner,
        )

        self.assertEqual(code, 0)
        prefixes = [tuple(call["args"][:3]) for call in runner.calls]
        self.assertNotIn(("git", "clean", "-fd"), prefixes)
        self.assertNotIn(("git", "reset", "--hard"), prefixes)


if __name__ == "__main__":
    unittest.main()
