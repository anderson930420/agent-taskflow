from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.local_cleanup_confirm import (
    LocalCleanupConfirmRequest,
    confirm_local_cleanup,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeCleanupRunner:
    def __init__(
        self,
        *,
        worktree_path: Path | None = None,
        branch_name: str = "task/AT-LC-001",
        status_short: str = "",
        worktree_registered: bool = True,
        branch_exists: bool = True,
        merged_into_base: bool = True,
        current_branch: str = "main",
        remote_exists: bool = True,
        remove_worktree_returncode: int = 0,
        branch_delete_returncode: int = 0,
        branch_delete_stderr: str = "",
    ) -> None:
        self.status_short = status_short
        self.worktree_path = worktree_path
        self.branch_name = branch_name
        self.worktree_registered = worktree_registered
        self.branch_exists = branch_exists
        self.merged_into_base = merged_into_base
        self.current_branch = current_branch
        self.remote_exists = remote_exists
        self.remove_worktree_returncode = remove_worktree_returncode
        self.branch_delete_returncode = branch_delete_returncode
        self.branch_delete_stderr = branch_delete_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "cwd": kwargs.get("cwd")})
        cwd = kwargs.get("cwd")
        cwd_path = Path(cwd) if cwd is not None else None

        if args[:3] == ["git", "worktree", "list"]:
            if not self.worktree_registered:
                return FakeCompletedProcess(returncode=0, stdout="")
            worktree_path = str(self.worktree_path or Path("/tmp/unused"))
            return FakeCompletedProcess(
                returncode=0,
                stdout=f"worktree {worktree_path}\nHEAD deadbeef\nbranch refs/heads/{self.branch_name}\n",
            )

        if args[:2] == ["git", "status"]:
            if cwd_path is None or not cwd_path.exists():
                return FakeCompletedProcess(returncode=128, stdout="", stderr="path missing")
            return FakeCompletedProcess(returncode=0, stdout=self.status_short)

        if args[:3] == ["git", "branch", "--list"]:
            stdout = f"{self.branch_name}\n" if self.branch_exists else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)

        if args[:3] == ["git", "branch", "--merged"]:
            stdout = f"{self.branch_name}\n" if self.merged_into_base and self.branch_exists else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)

        if args[:3] == ["git", "branch", "--show-current"]:
            return FakeCompletedProcess(returncode=0, stdout=f"{self.current_branch}\n" if self.current_branch else "")

        if args[:3] == ["git", "ls-remote", "--heads"]:
            stdout = f"deadbeef\trefs/heads/{self.branch_name}\n" if self.remote_exists else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)

        if args[:3] == ["git", "worktree", "remove"]:
            if self.remove_worktree_returncode == 0:
                worktree_path = Path(args[-1])
                if worktree_path.exists():
                    shutil.rmtree(worktree_path)
                return FakeCompletedProcess(returncode=0, stdout="removed\n")
            return FakeCompletedProcess(
                returncode=self.remove_worktree_returncode,
                stdout="",
                stderr="worktree remove failed",
            )

        if args[:3] == ["git", "branch", "-d"]:
            if self.branch_delete_returncode == 0:
                self.branch_exists = False
                return FakeCompletedProcess(returncode=0, stdout="deleted\n")
            return FakeCompletedProcess(
                returncode=self.branch_delete_returncode,
                stdout="",
                stderr=self.branch_delete_stderr or "branch delete failed",
            )

        raise AssertionError(f"unexpected command: {args}")
class LocalCleanupConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.worktree_root = self.repo / ".worktrees"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-LC-001"
        self.branch = f"task/{self.task_key}"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _worktree_path(self, task_key: str | None = None) -> Path:
        return self.worktree_root / (task_key or self.task_key)

    def _seed_task(
        self,
        *,
        worktree_path: Path | None = None,
        branch: str | None = None,
        worktree_exists: bool = True,
        status_short: str = "",
        merged_pr: bool = True,
        remote_exists: bool = True,
        include_draft_pr: bool = True,
        task_status: str = "waiting_approval",
        base_branch: str = "main",
    ) -> Path:
        worktree_path = worktree_path or self._worktree_path()
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Local cleanup task",
                status=task_status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self.task_key,
                repo_path=self.repo,
                worktree_path=worktree_path,
                branch=branch or self.branch,
                base_branch=base_branch,
                base_sha="base-sha",
                status="active",
            )
        )
        if worktree_exists:
            worktree_path.mkdir(parents=True, exist_ok=True)
            if status_short:
                (worktree_path / "dirty.txt").write_text(status_short, encoding="utf-8")
        if include_draft_pr:
            draft_pr_path = artifact_dir / "draft_pr.json"
            draft_pr_payload = {
                "kind": "draft_pr_created",
                "artifact_type": "draft_pr",
                "task_key": self.task_key,
                "repo": "anderson930420/agent-taskflow",
                "base_branch": base_branch,
                "head_branch": branch or self.branch,
                "title": "Local cleanup task",
                "body": "Draft PR body",
                "draft": True,
                "pr_number": 123,
                "pr_url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "issue_closed": False,
                "requires_human_confirmation": True,
            }
            draft_pr_path.write_text(json.dumps(draft_pr_payload, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "draft_pr", draft_pr_path)
            self.store.record_task_event(
                self.task_key,
                "draft_pr_created",
                "draft_pr_confirm",
                payload=draft_pr_payload,
            )

        offline_pr_json = self.root / "offline-pr.json"
        pr_payload = {
            "number": 123,
            "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
            "state": "MERGED" if merged_pr else "OPEN",
            "isDraft": False,
            "mergedAt": "2026-05-18T00:00:00Z" if merged_pr else None,
            "mergeCommit": {"oid": "deadbeef"} if merged_pr else None,
            "headRefName": branch or self.branch,
            "baseRefName": base_branch,
            "title": "Local cleanup task",
        }
        offline_pr_json.write_text(json.dumps(pr_payload, indent=2, sort_keys=True), encoding="utf-8")
        self.offline_pr_json = offline_pr_json
        self.remote_exists = remote_exists
        return artifact_dir

    def _request(
        self,
        *,
        dry_run: bool = False,
        confirm_local_cleanup: bool = False,
        delete_local_branch: bool = False,
        skip_local_branch_delete: bool = False,
        allow_dirty_worktree: bool = False,
        worktree_root: Path | None = None,
    ) -> LocalCleanupConfirmRequest:
        return LocalCleanupConfirmRequest(
            task_key=self.task_key,
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            worktree_root=worktree_root or self.worktree_root,
            remote="origin",
            offline_pr_json=self.offline_pr_json,
            dry_run=dry_run,
            confirm_local_cleanup=confirm_local_cleanup,
            delete_local_branch=delete_local_branch,
            skip_local_branch_delete=skip_local_branch_delete,
            allow_dirty_worktree=allow_dirty_worktree,
        )

    def test_missing_task_returns_not_found(self) -> None:
        result = confirm_local_cleanup(
            LocalCleanupConfirmRequest(
                task_key="AT-MISSING",
                repo_path=self.repo,
                db_path=self.db_path,
            ),
            store=self.store,
            runner=FakeCleanupRunner(worktree_path=self._worktree_path()),
        ).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertIn("Task not found", result["error"])

    def test_missing_confirm_refuses_actual_cleanup(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(self._request(), store=self.store, runner=runner).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("--confirm-local-cleanup", result["error"])
        self.assertFalse(any(call["args"][:3] == ["git", "worktree", "remove"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["git", "branch", "-d"] for call in runner.calls))

    def test_dry_run_does_not_remove_worktree_or_delete_branch(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(
            self._request(dry_run=True),
            store=self.store,
            runner=runner,
        ).to_dict()

        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["performed"])
        self.assertFalse(result["worktree"]["removed"])
        self.assertFalse(result["local_branch"]["deleted"])
        self.assertFalse(result["evidence"]["artifact_recorded"])
        self.assertFalse(result["evidence"]["event_recorded"])
        self.assertFalse(any(call["args"][:3] == ["git", "worktree", "remove"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["git", "branch", "-d"] for call in runner.calls))

    def test_missing_phase6a_cleanup_recommendation_blocks_cleanup(self) -> None:
        self._seed_task(merged_pr=False)
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("PR is not merged", result["error"])
        self.assertFalse(result["performed"])
        self.assertFalse(any(call["args"][:3] == ["git", "worktree", "remove"] for call in runner.calls))

    def test_pr_not_merged_blocks_cleanup(self) -> None:
        self._seed_task(merged_pr=False)
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["performed"])
        self.assertIn("PR is not merged", " ".join(result["blocking_warnings"]))

    def test_merged_pr_but_dirty_worktree_blocks_by_default(self) -> None:
        self._seed_task(status_short="?? dirty.txt\n")
        runner = FakeCleanupRunner(worktree_path=self._worktree_path(), status_short="?? dirty.txt\n")

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["performed"])
        self.assertIn("dirty", " ".join(result["blocking_warnings"]).lower())

    def test_dirty_worktree_can_proceed_only_with_allow_flag(self) -> None:
        self._seed_task(status_short="?? dirty.txt\n")
        runner = FakeCleanupRunner(worktree_path=self._worktree_path(), status_short="?? dirty.txt\n")

        result = confirm_local_cleanup(
            self._request(confirm_local_cleanup=True, allow_dirty_worktree=True, skip_local_branch_delete=True),
            store=self.store,
            runner=runner,
        ).to_dict()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial_cleanup")
        self.assertTrue(result["worktree"]["removed"])
        self.assertFalse(result["local_branch"]["deleted"])

    def test_worktree_path_missing_blocks_cleanup(self) -> None:
        self._seed_task(worktree_exists=False)
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("local worktree removal", result["error"])
        self.assertFalse(result["performed"])

    def test_worktree_path_outside_expected_root_blocks_cleanup(self) -> None:
        outside = self.root / "outside" / self.task_key
        self._seed_task(worktree_path=outside)
        runner = FakeCleanupRunner(worktree_path=outside)

        result = confirm_local_cleanup(
            self._request(confirm_local_cleanup=True, worktree_root=self.worktree_root),
            store=self.store,
            runner=runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("outside the expected worktree root", " ".join(result["blocking_warnings"]))

    def test_worktree_path_equal_repo_root_blocks_cleanup(self) -> None:
        self._seed_task(worktree_path=self.repo)
        runner = FakeCleanupRunner(worktree_path=self.repo)

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("repository root", " ".join(result["blocking_warnings"]))

    def test_main_branch_blocks_branch_deletion(self) -> None:
        self._seed_task(branch="main")
        runner = FakeCleanupRunner(
            worktree_path=self._worktree_path(),
            branch_name="main",
            branch_exists=True,
            merged_into_base=True,
            current_branch="main",
        )

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("protected", " ".join(result["blocking_warnings"]).lower())

    def test_branch_not_merged_into_base_blocks_branch_deletion(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path(), merged_into_base=False)

        result = confirm_local_cleanup(self._request(confirm_local_cleanup=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "partial_cleanup")
        self.assertTrue(result["worktree"]["removed"])
        self.assertFalse(result["local_branch"]["deleted"])
        self.assertFalse(result["local_branch"]["safe_to_delete"])

    def test_branch_deletion_uses_git_branch_d_only(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        confirm_local_cleanup(
            self._request(confirm_local_cleanup=True),
            store=self.store,
            runner=runner,
        )

        self.assertIn(["git", "branch", "-d", self.branch], [call["args"] for call in runner.calls])
        self.assertFalse(any("-D" in arg for call in runner.calls for arg in call["args"]))
        self.assertFalse(any("--force" in arg for call in runner.calls for arg in call["args"]))

    def test_successful_cleanup_removes_worktree_and_deletes_branch(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(
            self._request(confirm_local_cleanup=True),
            store=self.store,
            runner=runner,
        ).to_dict()

        self.assertEqual(result["status"], "local_cleanup_completed")
        self.assertTrue(result["worktree"]["removed"])
        self.assertFalse(self._worktree_path().exists())
        self.assertTrue(result["local_branch"]["deleted"])
        self.assertFalse(result["local_branch"]["exists_after"])
        self.assertTrue(result["evidence"]["artifact_recorded"])
        self.assertTrue(result["evidence"]["event_recorded"])
        artifact_path = self.artifact_root / "local_cleanup" / self.task_key / "local_cleanup.json"
        self.assertTrue(artifact_path.exists())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertFalse(payload["task_completed"])
        self.assertFalse(payload["remote_branch_deleted"])
        self.assertFalse(payload["issue_closed"])

    def test_partial_cleanup_reports_branch_skip_accurately(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        result = confirm_local_cleanup(
            self._request(confirm_local_cleanup=True, skip_local_branch_delete=True),
            store=self.store,
            runner=runner,
        ).to_dict()

        self.assertEqual(result["status"], "partial_cleanup")
        self.assertTrue(result["worktree"]["removed"])
        self.assertFalse(result["local_branch"]["deleted"])
        self.assertTrue(result["evidence"]["artifact_recorded"])
        self.assertTrue(result["evidence"]["event_recorded"])

    def test_evidence_is_recorded_only_after_actual_cleanup(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))

        result = confirm_local_cleanup(
            self._request(confirm_local_cleanup=True, skip_local_branch_delete=True),
            store=self.store,
            runner=FakeCleanupRunner(worktree_path=self._worktree_path()),
        ).to_dict()

        self.assertEqual(result["status"], "partial_cleanup")
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), before_artifacts + 1)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), before_events + 1)

    def test_safety_block_remains_remote_false_and_no_task_updates(self) -> None:
        self._seed_task()
        result = confirm_local_cleanup(
            self._request(confirm_local_cleanup=True),
            store=self.store,
            runner=FakeCleanupRunner(worktree_path=self._worktree_path()),
        ).to_dict()

        self.assertFalse(result["safety"]["remote_branch_deleted"])
        self.assertFalse(result["safety"]["issue_closed"])
        self.assertFalse(result["safety"]["task_status_changed"])

    def test_no_forbidden_helpers_are_called(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())

        confirm_local_cleanup(
            self._request(confirm_local_cleanup=True, skip_local_branch_delete=True),
            store=self.store,
            runner=runner,
        )

        commands = [" ".join(call["args"]) for call in runner.calls]
        forbidden = [
            "rm -rf",
            "git branch -D",
            "git push",
            "git push --delete",
            "git clean",
            "git reset --hard",
            "git merge",
            "git worktree prune",
            "worktree remove --force",
            "gh pr merge",
            "gh issue close",
            "gh pr close",
        ]
        for token in forbidden:
            self.assertFalse(any(token in command for command in commands), token)
