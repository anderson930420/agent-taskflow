from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import confirm_remote_branch_cleanup as script


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeRemoteCleanupRunner:
    def __init__(
        self,
        *,
        branch_name: str = "task/AT-RC-SCRIPT-001",
        branch_exists: bool = True,
        merged_into_base: bool = True,
        remote_exists: bool = True,
        push_returncode: int = 0,
        push_stdout: str = "deleted\n",
        push_stderr: str = "",
    ) -> None:
        self.branch_name = branch_name
        self.branch_exists = branch_exists
        self.merged_into_base = merged_into_base
        self.remote_exists = remote_exists
        self.push_returncode = push_returncode
        self.push_stdout = push_stdout
        self.push_stderr = push_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "cwd": kwargs.get("cwd")})
        if args[:2] == ["git", "status"]:
            return FakeCompletedProcess(returncode=0, stdout="")
        if args[:3] == ["git", "branch", "--list"]:
            stdout = f"{self.branch_name}\n" if self.branch_exists else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)
        if args[:3] == ["git", "branch", "--merged"]:
            stdout = f"{self.branch_name}\n" if self.branch_exists and self.merged_into_base else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)
        if args[:3] == ["git", "ls-remote", "--heads"]:
            if not self.remote_exists or not self.branch_exists or args[-1] != self.branch_name:
                return FakeCompletedProcess(returncode=0, stdout="")
            return FakeCompletedProcess(
                returncode=0,
                stdout=f"deadbeef\trefs/heads/{self.branch_name}\n",
            )
        if args[:3] == ["git", "push", "origin"] and "--delete" in args:
            if self.push_returncode == 0 and self.remote_exists:
                self.branch_exists = False
                return FakeCompletedProcess(returncode=0, stdout=self.push_stdout)
            return FakeCompletedProcess(
                returncode=self.push_returncode,
                stdout="",
                stderr=self.push_stderr or "push failed",
            )
        raise AssertionError(f"unexpected command: {args}")


class ConfirmRemoteBranchCleanupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.repo / ".worktrees" / "AT-RC-SCRIPT-001"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-RC-SCRIPT-001"
        self.branch = f"task/{self.task_key}"
        self.repo.mkdir(parents=True, exist_ok=True)
        self.worktree.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        branch: str | None = None,
        base_branch: str = "main",
        merged_pr: bool = True,
        remote_exists: bool = True,
        branch_exists: bool = True,
        task_status: str = "waiting_approval",
    ) -> None:
        branch_name = branch or self.branch
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Remote branch cleanup script task",
                status=task_status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self.task_key,
                repo_path=self.repo,
                worktree_path=self.worktree,
                branch=branch_name,
                base_branch=base_branch,
                base_sha="base-sha",
                status="active",
            )
        )
        draft_pr_path = artifact_dir / "draft_pr.json"
        draft_pr_payload = {
            "schema_version": "1",
            "artifact_type": "draft_pr",
            "kind": "draft_pr_created",
            "task_key": self.task_key,
            "repo": "anderson930420/agent-taskflow",
            "base_branch": base_branch,
            "head_branch": branch_name,
            "title": "Remote branch cleanup script task",
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

        local_cleanup_dir = self.artifact_root / "local_cleanup" / self.task_key
        local_cleanup_dir.mkdir(parents=True, exist_ok=True)
        local_cleanup_path = local_cleanup_dir / "local_cleanup.json"
        local_cleanup_payload = {
            "schema_version": "1",
            "artifact_type": "local_cleanup",
            "kind": "local_cleanup_completed",
            "task_key": self.task_key,
            "task_status": task_status,
            "worktree_path": str(self.worktree),
            "local_branch": branch_name,
            "worktree_removed": True,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_completed": False,
            "task_archived": False,
            "cleanup_scope": "local",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-local-cleanup",
        }
        local_cleanup_path.write_text(json.dumps(local_cleanup_payload, sort_keys=True), encoding="utf-8")
        self.store.record_task_artifact(self.task_key, "local_cleanup", local_cleanup_path)
        self.store.record_task_event(
            self.task_key,
            "local_cleanup_completed",
            "local_cleanup_confirm",
            payload=local_cleanup_payload,
        )

        offline_pr_json = self.root / "offline-pr.json"
        offline_pr_json.write_text(
            json.dumps(
                {
                    "number": 123,
                    "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                    "state": "MERGED" if merged_pr else "OPEN",
                    "isDraft": False,
                    "mergedAt": "2026-05-18T00:00:00Z" if merged_pr else None,
                    "mergeCommit": {"oid": "deadbeef"} if merged_pr else None,
                    "headRefName": branch_name,
                    "baseRefName": base_branch,
                    "title": "Remote branch cleanup script task",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.offline_pr_json = offline_pr_json
        self.runner = FakeRemoteCleanupRunner(
            branch_name=branch_name,
            branch_exists=branch_exists,
            merged_into_base=True,
            remote_exists=remote_exists,
        )

    def _run_main(self, argv: list[str], *, runner: FakeRemoteCleanupRunner | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                exit_code = script.main(argv, runner=runner)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _base_args(self) -> list[str]:
        return [
            "--task-key",
            self.task_key,
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
            "--offline-pr-json",
            str(self.offline_pr_json),
        ]

    def test_script_requires_task_key(self) -> None:
        exit_code, _stdout, stderr = self._run_main(
            [
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--json",
            ]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("--task-key", stderr)

    def test_script_requires_confirm_remote_branch_delete_for_actual_deletion(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--json"], runner=self.runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-remote-branch-delete", payload["error"])
        self.assertFalse(any(call["args"][:3] == ["git", "push", "origin"] for call in self.runner.calls))

    def test_script_supports_dry_run_without_remote_deletion(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run", "--json"],
            runner=self.runner,
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["remote_branch"]["deleted"])
        self.assertFalse(any(call["args"][:3] == ["git", "push", "origin"] for call in self.runner.calls))

    def test_script_prints_valid_json(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-remote-branch-delete", "--json"],
            runner=self.runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["task_key"], self.task_key)
        self.assertEqual(payload["status"], "remote_branch_cleanup_completed")

    def test_script_handles_missing_db_without_creating_db_file(self) -> None:
        missing_db = self.root / "missing.db"
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                self.task_key,
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(missing_db),
                "--json",
            ]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "not_found")
        self.assertFalse(missing_db.exists())

    def test_script_blocks_protected_branches(self) -> None:
        self._seed_task(branch="main")
        runner = FakeRemoteCleanupRunner(branch_name="main")
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-remote-branch-delete", "--json"],
            runner=runner,
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("protected", " ".join(payload["blocking_warnings"]).lower())

    def test_script_blocks_invalid_branch_names(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args()
            + ["--branch", "bad branch", "--confirm-remote-branch-delete", "--json"],
            runner=self.runner,
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("branch", payload["error"].lower())

    def test_script_does_not_update_task_status(self) -> None:
        self._seed_task()
        before_status = self.store.get_task(self.task_key).status

        exit_code, _stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-remote-branch-delete", "--json"],
            runner=self.runner,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_task(self.task_key).status, before_status)

    def test_script_does_not_delete_local_branch(self) -> None:
        self._seed_task()

        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-remote-branch-delete", "--json"],
            runner=self.runner,
        )

        self.assertEqual(exit_code, 0)
        commands = [" ".join(call["args"]) for call in self.runner.calls]
        self.assertFalse(any("git branch -d" in command for command in commands))
        self.assertFalse(any("git branch -D" in command for command in commands))

    def test_script_does_not_remove_worktree(self) -> None:
        self._seed_task()

        exit_code, _stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-remote-branch-delete", "--json"],
            runner=self.runner,
        )

        self.assertEqual(exit_code, 0)
        commands = [" ".join(call["args"]) for call in self.runner.calls]
        self.assertFalse(any("git worktree remove" in command for command in commands))

    def test_script_does_not_close_issue_archive_task_or_merge_approve(self) -> None:
        self._seed_task()

        exit_code, _stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-remote-branch-delete", "--json"],
            runner=self.runner,
        )

        self.assertEqual(exit_code, 0)
        commands = [" ".join(call["args"]) for call in self.runner.calls]
        self.assertFalse(any("gh issue close" in command for command in commands))
        self.assertFalse(any("gh pr merge" in command for command in commands))
        self.assertFalse(any("gh pr review" in command for command in commands))
        self.assertFalse(any("--force" in command for command in commands))
        self.assertFalse(any("--force-with-lease" in command for command in commands))
        self.assertFalse(any("git push --mirror" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
