"""Tests for agent_taskflow.branch_push."""

from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.branch_push import (
    BranchPushError,
    BranchPushRequest,
    push_task_branch,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeRunner:
    def __init__(
        self,
        *,
        branch: str = "task/AT-PUSH-001",
        status: str = "",
        ahead_count: str = "2\n",
        push_returncode: int = 0,
        push_stderr: str = "",
    ) -> None:
        self.branch = branch
        self.status = status
        self.ahead_count = ahead_count
        self.push_returncode = push_returncode
        self.push_stderr = push_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return FakeCompletedProcess(returncode=0, stdout=f"{self.branch}\n")
        if args == ["git", "status", "--short"]:
            return FakeCompletedProcess(returncode=0, stdout=self.status)
        if args[:3] == ["git", "rev-list", "--count"]:
            return FakeCompletedProcess(returncode=0, stdout=self.ahead_count)
        if args[:2] == ["git", "push"]:
            return FakeCompletedProcess(
                returncode=self.push_returncode,
                stdout="",
                stderr=self.push_stderr,
            )
        return FakeCompletedProcess(returncode=99, stdout="", stderr="unexpected command")


class BranchPushTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.repo / ".worktrees" / "AT-PUSH-001"
        self.db_path = self.root / "state.db"
        self.artifact_dir = self.root / "artifacts" / "AT-PUSH-001"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._create_valid_task()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_valid_task(
        self,
        *,
        task_key: str = "AT-PUSH-001",
        with_worktree: bool = True,
        branch: str = "task/AT-PUSH-001",
        base_branch: str = "main",
        base_sha: str | None = "abc123",
        worktree_exists: bool = True,
    ) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        if worktree_exists:
            self.worktree.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Branch push test",
                status="waiting_approval",
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )
        if with_worktree:
            self.store.upsert_task_worktree(
                TaskWorktreeRecord(
                    task_key=task_key,
                    repo_path=self.repo,
                    worktree_path=self.worktree,
                    branch=branch,
                    base_branch=base_branch,
                    base_sha=base_sha,
                    status="active",
                )
            )

    def _request(
        self,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        task_key: str = "AT-PUSH-001",
    ) -> BranchPushRequest:
        return BranchPushRequest(
            task_key=task_key,
            db_path=self.db_path,
            dry_run=dry_run,
            confirm_push=confirm,
        )

    def test_dry_run_without_confirm_never_calls_git_push(self) -> None:
        runner = FakeRunner()
        result = push_task_branch(self._request(), store=self.store, runner=runner)

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.pushed)
        self.assertIn("git push --set-upstream origin task/AT-PUSH-001", result.command_preview)
        self.assertFalse(any(call["args"][:2] == ["git", "push"] for call in runner.calls))

    def test_explicit_dry_run_with_confirm_still_never_calls_git_push(self) -> None:
        runner = FakeRunner()
        result = push_task_branch(
            self._request(dry_run=True, confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(any(call["args"][:2] == ["git", "push"] for call in runner.calls))

    def test_real_push_requires_confirm_push(self) -> None:
        runner = FakeRunner()
        result = push_task_branch(
            self._request(dry_run=True, confirm=False),
            store=self.store,
            runner=runner,
        )

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(any(call["args"][:2] == ["git", "push"] for call in runner.calls))

    def test_real_push_executes_exact_command_with_shell_false(self) -> None:
        runner = FakeRunner()
        result = push_task_branch(
            self._request(dry_run=False, confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertEqual(result.status, "pushed")
        push_calls = [call for call in runner.calls if call["args"][:2] == ["git", "push"]]
        self.assertEqual(len(push_calls), 1)
        self.assertEqual(
            push_calls[0]["args"],
            ["git", "push", "--set-upstream", "origin", "task/AT-PUSH-001"],
        )
        self.assertIs(push_calls[0]["kwargs"]["shell"], False)
        self.assertEqual(push_calls[0]["kwargs"]["cwd"], self.worktree)

    def test_rejects_missing_task(self) -> None:
        with self.assertRaisesRegex(BranchPushError, "Task not found"):
            push_task_branch(
                self._request(task_key="AT-MISSING"),
                store=self.store,
                runner=FakeRunner(),
            )

    def test_rejects_missing_worktree_record(self) -> None:
        self._create_valid_task(task_key="AT-NO-WORKTREE", with_worktree=False)

        with self.assertRaisesRegex(BranchPushError, "TaskWorktreeRecord missing"):
            push_task_branch(
                self._request(task_key="AT-NO-WORKTREE"),
                store=self.store,
                runner=FakeRunner(),
            )

    def test_rejects_missing_worktree_path(self) -> None:
        self._create_valid_task(task_key="AT-MISSING-PATH", worktree_exists=False)
        shutil.rmtree(self.worktree)

        with self.assertRaisesRegex(BranchPushError, "Worktree path is missing"):
            push_task_branch(
                self._request(task_key="AT-MISSING-PATH"),
                store=self.store,
                runner=FakeRunner(),
            )

    def test_rejects_protected_branches(self) -> None:
        for branch in ("main", "master", "trunk"):
            with self.subTest(branch=branch):
                task_key = f"AT-{branch.upper()}"
                self._create_valid_task(task_key=task_key, branch=branch, base_branch="develop")
                with self.assertRaisesRegex(BranchPushError, "protected branch"):
                    push_task_branch(
                        self._request(task_key=task_key),
                        store=self.store,
                        runner=FakeRunner(branch=branch),
                    )

    def test_rejects_base_branch(self) -> None:
        self._create_valid_task(task_key="AT-BASE", branch="release", base_branch="release")

        with self.assertRaisesRegex(BranchPushError, "protected branch"):
            push_task_branch(
                self._request(task_key="AT-BASE"),
                store=self.store,
                runner=FakeRunner(branch="release"),
            )

    def test_rejects_current_branch_mismatch(self) -> None:
        runner = FakeRunner(branch="task/OTHER")

        with self.assertRaisesRegex(BranchPushError, "does not match task branch"):
            push_task_branch(self._request(), store=self.store, runner=runner)

    def test_rejects_dirty_worktree(self) -> None:
        runner = FakeRunner(status=" M file.txt\n")

        with self.assertRaisesRegex(BranchPushError, "worktree has uncommitted changes"):
            push_task_branch(self._request(), store=self.store, runner=runner)

    def test_rejects_zero_ahead_commits_when_base_sha_exists(self) -> None:
        runner = FakeRunner(ahead_count="0\n")

        with self.assertRaisesRegex(BranchPushError, "no commits beyond base_sha"):
            push_task_branch(self._request(), store=self.store, runner=runner)

    def test_rejects_force_push_flags_if_present(self) -> None:
        from agent_taskflow.branch_push import _ensure_no_unsafe_push_flags

        with self.assertRaisesRegex(BranchPushError, "force"):
            _ensure_no_unsafe_push_flags(["git", "push", "--force", "origin", "task/x"])

    def test_records_event_and_artifact_after_successful_push(self) -> None:
        result = push_task_branch(
            self._request(dry_run=False, confirm=True),
            store=self.store,
            runner=FakeRunner(),
        )

        self.assertTrue(result.event_recorded)
        self.assertTrue(result.artifact_recorded)
        self.assertTrue(result.branch_push_json_path and result.branch_push_json_path.is_file())
        events = self.store.list_task_events("AT-PUSH-001")
        artifacts = self.store.list_task_artifacts("AT-PUSH-001")
        self.assertTrue(any(event.event_type == "branch_pushed" for event in events))
        self.assertTrue(any(artifact.artifact_type == "branch_push" for artifact in artifacts))
        payload = json.loads(result.branch_push_json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "branch_pushed")
        self.assertTrue(payload["safety"]["pushed"])
        self.assertFalse(payload["safety"]["force_pushed"])

    def test_dry_run_records_no_event_or_artifact(self) -> None:
        result = push_task_branch(self._request(), store=self.store, runner=FakeRunner())

        self.assertFalse(result.event_recorded)
        self.assertFalse(result.artifact_recorded)
        self.assertFalse(self.store.list_task_events("AT-PUSH-001"))
        self.assertFalse(self.store.list_task_artifacts("AT-PUSH-001"))

    def test_git_push_failure_is_blocked(self) -> None:
        runner = FakeRunner(push_returncode=1, push_stderr="remote rejected")

        with self.assertRaisesRegex(BranchPushError, "git push failed"):
            push_task_branch(
                self._request(dry_run=False, confirm=True),
                store=self.store,
                runner=runner,
            )

    def test_command_preview_has_push_but_result_is_not_pushed_on_dry_run(self) -> None:
        result = push_task_branch(self._request(), store=self.store, runner=FakeRunner())

        self.assertIn("git push", result.command_preview)
        self.assertFalse(result.pushed)
        self.assertFalse(result.github_mutated)


if __name__ == "__main__":
    unittest.main()
