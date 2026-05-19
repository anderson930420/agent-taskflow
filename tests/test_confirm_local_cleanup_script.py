from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import confirm_local_cleanup as script


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
        branch_name: str = "task/AT-SCRIPT-001",
        status_short: str = "",
        merged_into_base: bool = True,
        current_branch: str = "main",
        remove_worktree_returncode: int = 0,
        branch_delete_returncode: int = 0,
    ) -> None:
        self.worktree_path = worktree_path
        self.branch_name = branch_name
        self.status_short = status_short
        self.merged_into_base = merged_into_base
        self.current_branch = current_branch
        self.remove_worktree_returncode = remove_worktree_returncode
        self.branch_delete_returncode = branch_delete_returncode
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append(args)
        cwd = kwargs.get("cwd")
        cwd_path = Path(cwd) if cwd is not None else None

        if args[:3] == ["git", "worktree", "list"]:
            if not self.worktree_path:
                return FakeCompletedProcess(returncode=0, stdout="")
            return FakeCompletedProcess(
                returncode=0,
                stdout=f"worktree {self.worktree_path}\nHEAD deadbeef\nbranch refs/heads/{self.branch_name}\n",
            )
        if args[:2] == ["git", "status"]:
            if cwd_path is None or not cwd_path.exists():
                return FakeCompletedProcess(returncode=128, stdout="", stderr="path missing")
            return FakeCompletedProcess(returncode=0, stdout=self.status_short)
        if args[:3] == ["git", "branch", "--list"]:
            return FakeCompletedProcess(returncode=0, stdout=f"{self.branch_name}\n")
        if args[:3] == ["git", "branch", "--merged"]:
            stdout = f"{self.branch_name}\n" if self.merged_into_base else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout)
        if args[:3] == ["git", "branch", "--show-current"]:
            return FakeCompletedProcess(returncode=0, stdout=f"{self.current_branch}\n")
        if args[:3] == ["git", "ls-remote", "--heads"]:
            return FakeCompletedProcess(returncode=0, stdout="deadbeef\trefs/heads/main\n")
        if args[:3] == ["git", "worktree", "remove"]:
            if self.remove_worktree_returncode == 0:
                path = Path(args[-1])
                if path.exists():
                    shutil.rmtree(path)
                return FakeCompletedProcess(returncode=0, stdout="removed\n")
            return FakeCompletedProcess(returncode=self.remove_worktree_returncode, stdout="", stderr="remove failed")
        if args[:3] == ["git", "branch", "-d"]:
            if self.branch_delete_returncode == 0:
                return FakeCompletedProcess(returncode=0, stdout="deleted\n")
            return FakeCompletedProcess(returncode=self.branch_delete_returncode, stdout="", stderr="delete failed")
        raise AssertionError(f"unexpected command: {args}")


class ConfirmLocalCleanupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.worktree_root = self.repo / ".worktrees"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-SCRIPT-001"
        self.branch = f"task/{self.task_key}"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _worktree_path(self, task_key: str | None = None) -> Path:
        return self.worktree_root / (task_key or self.task_key)

    def _seed_task(
        self,
        *,
        branch: str | None = None,
        worktree_path: Path | None = None,
        status_short: str = "",
        merged_pr: bool = True,
        include_draft_pr: bool = True,
        task_status: str = "waiting_approval",
    ) -> None:
        worktree_path = worktree_path or self._worktree_path()
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Script cleanup task",
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
                base_branch="main",
                base_sha="base-sha",
                status="active",
            )
        )
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
                "base_branch": "main",
                "head_branch": branch or self.branch,
                "title": "Script cleanup task",
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
        offline_pr_json.write_text(
            json.dumps(
                {
                    "number": 123,
                    "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                    "state": "MERGED" if merged_pr else "OPEN",
                    "isDraft": False,
                    "mergedAt": "2026-05-18T00:00:00Z" if merged_pr else None,
                    "mergeCommit": {"oid": "deadbeef"} if merged_pr else None,
                    "headRefName": branch or self.branch,
                    "baseRefName": "main",
                    "title": "Script cleanup task",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.offline_pr_json = offline_pr_json

    def _run_main(self, argv: list[str], *, runner: FakeCleanupRunner | None = None) -> tuple[int, str, str]:
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
            "--worktree-root",
            str(self.worktree_root),
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

    def test_script_requires_confirm_for_actual_cleanup(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--json"], runner=FakeCleanupRunner(worktree_path=self._worktree_path()))

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-local-cleanup", payload["error"])

    def test_script_supports_dry_run_without_cleanup(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--dry-run", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["worktree"]["removed"])
        self.assertFalse(payload["local_branch"]["deleted"])
        self.assertFalse(any(call[:3] == ["git", "worktree", "remove"] for call in runner.calls))

    def test_script_prints_valid_json(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        self.assertIn("task_key", json.loads(stdout))
        self.assertEqual(exit_code, 0)

    def test_script_handles_missing_db_without_creating_db_file(self) -> None:
        self.db_path.unlink()
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                self.task_key,
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--json",
            ]
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertFalse(self.db_path.exists())
        self.assertEqual(payload["status"], "not_found")

    def test_script_blocks_unsafe_worktree_paths(self) -> None:
        outside = self.root / "outside" / self.task_key
        self._seed_task(worktree_path=outside)
        runner = FakeCleanupRunner(worktree_path=outside)
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("outside the expected worktree root", " ".join(payload["blocking_warnings"]))

    def test_script_blocks_main_branch_deletion(self) -> None:
        self._seed_task(branch="main")
        runner = FakeCleanupRunner(worktree_path=self._worktree_path(), branch_name="main", current_branch="main")
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("protected", " ".join(payload["blocking_warnings"]).lower())

    def test_script_does_not_update_task_status(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["summary"]["task_status_changed"], False)
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")

    def test_script_does_not_delete_remote_branch(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["summary"]["remote_branch_deleted"])
        self.assertFalse(any("push" in " ".join(call) for call in runner.calls))

    def test_script_does_not_close_issue_or_archive_task(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["summary"]["issue_closed"])
        self.assertFalse(payload["summary"]["task_archived"])

    def test_script_does_not_merge_or_approve(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["approved"])

    def test_script_does_not_use_force_delete(self) -> None:
        self._seed_task()
        runner = FakeCleanupRunner(worktree_path=self._worktree_path())
        self._run_main(self._base_args() + ["--confirm-local-cleanup", "--json"], runner=runner)

        commands = [" ".join(call) for call in runner.calls]
        self.assertFalse(any("--force" in command for command in commands))
        self.assertFalse(any("branch -D" in command for command in commands))
