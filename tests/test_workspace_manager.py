from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.workspace_manager import (
    WORKSPACE_BLOCKED,
    WORKSPACE_PREPARED,
    WORKSPACE_REUSED,
    WorkspaceManager,
    WorkspacePreparationRequest,
    prepare_task_workspace,
)


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git(["init"], self.repo)
        self._git(["config", "user.email", "agent-taskflow@example.invalid"], self.repo)
        self._git(["config", "user.name", "Agent Taskflow"], self.repo)
        (self.repo / "README.md").write_text("# test repo\n", encoding="utf-8")
        self._git(["add", "README.md"], self.repo)
        self._git(["commit", "-m", "initial"], self.repo)
        self._git(["branch", "-M", "main"], self.repo)
        self.base_sha = self._git(["rev-parse", "main"], self.repo).stdout.strip()
        self.manager = WorkspaceManager()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed

    def request(
        self,
        task_key: str = "AT-WS-001",
        *,
        repo_path: Path | None = None,
        worktree_root: Path | None = None,
        branch: str | None = None,
    ) -> WorkspacePreparationRequest:
        return WorkspacePreparationRequest(
            task_key=task_key,
            repo_path=repo_path or self.repo,
            worktree_root=worktree_root,
            branch=branch,
        )

    def test_successful_workspace_creation_captures_base_sha(self) -> None:
        result = self.manager.prepare(self.request())

        self.assertEqual(result.status, WORKSPACE_PREPARED)
        self.assertEqual(result.task_key, "AT-WS-001")
        self.assertEqual(result.repo_path, self.repo)
        self.assertEqual(result.worktree_path, self.repo / ".worktrees" / "AT-WS-001")
        self.assertEqual(result.branch, "task/AT-WS-001")
        self.assertEqual(result.base_branch, "main")
        self.assertEqual(result.base_sha, self.base_sha)
        self.assertTrue((result.worktree_path / "README.md").is_file())

    def test_second_prepare_reuses_clean_registered_worktree(self) -> None:
        first = self.manager.prepare(self.request())
        second = self.manager.prepare(self.request())

        self.assertEqual(first.status, WORKSPACE_PREPARED)
        self.assertEqual(second.status, WORKSPACE_REUSED)
        self.assertEqual(second.worktree_path, first.worktree_path)
        self.assertEqual(second.base_sha, self.base_sha)

    def test_invalid_repo_is_blocked(self) -> None:
        not_repo = self.root / "not-repo"
        not_repo.mkdir()

        result = self.manager.prepare(self.request(repo_path=not_repo))

        self.assertEqual(result.status, WORKSPACE_BLOCKED)
        self.assertIn("not a git repository", result.summary)

    def test_worktree_path_escape_is_blocked(self) -> None:
        result = self.manager.prepare(
            self.request(worktree_root=self.root / "outside-worktrees")
        )

        self.assertEqual(result.status, WORKSPACE_BLOCKED)
        self.assertIn(".worktrees", result.summary)
        self.assertFalse((self.root / "outside-worktrees" / "AT-WS-001").exists())

    def test_main_repo_path_is_blocked(self) -> None:
        result = self.manager.prepare(
            self.request(task_key=self.repo.name, worktree_root=self.repo.parent)
        )

        self.assertEqual(result.status, WORKSPACE_BLOCKED)
        self.assertIn("main repo path", result.summary)

    def test_existing_unregistered_path_is_blocked(self) -> None:
        target = self.repo / ".worktrees" / "AT-WS-001"
        target.mkdir(parents=True)

        result = self.manager.prepare(self.request())

        self.assertEqual(result.status, WORKSPACE_BLOCKED)
        self.assertIn("already exists but is not registered", result.summary)

    def test_dirty_existing_worktree_is_blocked(self) -> None:
        first = self.manager.prepare(self.request())
        (first.worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        result = self.manager.prepare(self.request())

        self.assertEqual(result.status, WORKSPACE_BLOCKED)
        self.assertIn("dirty", result.summary)

    def test_existing_branch_without_matching_worktree_is_blocked(self) -> None:
        self._git(["branch", "task/AT-WS-001", "main"], self.repo)

        result = self.manager.prepare(self.request())

        self.assertEqual(result.status, WORKSPACE_BLOCKED)
        self.assertIn("branch already exists", result.summary)

    def test_prepare_task_workspace_records_store_worktree_with_base_sha(self) -> None:
        db_path = self.root / "state.db"
        store = TaskMirrorStore(db_path)
        store.init_db()
        store.upsert_task(
            TaskRecord(
                task_key="AT-WS-001",
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
            )
        )

        result = prepare_task_workspace(self.request(), store=store)

        self.assertEqual(result.status, WORKSPACE_PREPARED)
        record = store.get_task_worktree("AT-WS-001")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.worktree_path, self.repo / ".worktrees" / "AT-WS-001")
        self.assertEqual(record.branch, "task/AT-WS-001")
        self.assertEqual(record.base_branch, "main")
        self.assertEqual(record.base_sha, self.base_sha)
        self.assertEqual(record.status, "active")


if __name__ == "__main__":
    unittest.main()
