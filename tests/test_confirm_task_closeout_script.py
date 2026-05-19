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
from scripts import confirm_task_closeout as script


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeCloseoutRunner:
    def __init__(self, *, merged: bool = True, remote_exists: bool = False) -> None:
        self.merged = merged
        self.remote_exists = remote_exists
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "cwd": kwargs.get("cwd")})
        if args[:3] == ["gh", "pr", "view"]:
            payload = {
                "number": 22,
                "url": "https://github.com/anderson930420/agent-taskflow/pull/22",
                "state": "MERGED" if self.merged else "OPEN",
                "isDraft": False,
                "mergedAt": "2026-05-18T00:00:00Z" if self.merged else None,
                "mergeCommit": {"oid": "deadbeef"} if self.merged else None,
                "headRefName": "task/AT-CC-SCRIPT-001",
                "baseRefName": "main",
                "title": "Script task closeout candidate",
            }
            return FakeCompletedProcess(returncode=0, stdout=json.dumps(payload))
        if args[:3] == ["git", "ls-remote", "--heads"]:
            if self.remote_exists:
                return FakeCompletedProcess(returncode=0, stdout="deadbeef\trefs/heads/task/AT-CC-SCRIPT-001\n")
            return FakeCompletedProcess(returncode=0, stdout="")
        raise AssertionError(f"unexpected command: {args}")


class ConfirmTaskCloseoutScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-CC-SCRIPT-001"
        self.branch = f"task/{self.task_key}"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        include_draft_pr: bool = True,
        include_local_cleanup: bool = True,
        include_remote_cleanup: bool = True,
        task_status: str = "waiting_approval",
    ) -> None:
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Script task closeout candidate",
                status=task_status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self.task_key,
                repo_path=self.repo,
                worktree_path=self.repo / ".worktrees" / self.task_key,
                branch=self.branch,
                base_branch="main",
                base_sha="base-sha",
                status="active",
            )
        )

        if include_draft_pr:
            draft_path = artifact_dir / "draft_pr.json"
            draft_payload = {
                "schema_version": "1",
                "artifact_type": "draft_pr",
                "kind": "draft_pr_created",
                "task_key": self.task_key,
                "repo": "anderson930420/agent-taskflow",
                "base_branch": "main",
                "head_branch": self.branch,
                "title": "Script task closeout candidate",
                "body": "Draft PR body",
                "draft": True,
                "pr_number": 22,
                "pr_url": "https://github.com/anderson930420/agent-taskflow/pull/22",
                "verified": True,
                "pr_created": True,
                "draft_pr_created": True,
                "merged": False,
                "issue_closed": False,
                "requires_human_confirmation": True,
            }
            draft_path.write_text(json.dumps(draft_payload, indent=2, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "draft_pr", draft_path)
            self.store.record_task_event(
                self.task_key,
                "draft_pr_created",
                "draft_pr_confirm",
                payload=draft_payload,
            )

        if include_local_cleanup:
            local_path = artifact_dir / "local_cleanup.json"
            local_payload = {
                "schema_version": "1",
                "artifact_type": "local_cleanup",
                "kind": "local_cleanup_completed",
                "task_key": self.task_key,
                "task_status": task_status,
                "worktree_path": str(self.repo / ".worktrees" / self.task_key),
                "branch": self.branch,
                "local_branch": self.branch,
                "worktree_removed": True,
                "local_branch_deleted": False,
                "issue_closed": False,
                "task_status_changed": False,
                "task_completed": False,
                "task_archived": False,
                "cleanup_scope": "local",
                "requires_human_confirmation": True,
                "confirmation_flag": "--confirm-local-cleanup",
            }
            local_path.write_text(json.dumps(local_payload, indent=2, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "local_cleanup", local_path)
            self.store.record_task_event(
                self.task_key,
                "local_cleanup_completed",
                "local_cleanup_confirm",
                payload=local_payload,
            )

        if include_remote_cleanup:
            remote_path = artifact_dir / "remote_branch_cleanup.json"
            remote_payload = {
                "schema_version": "1",
                "artifact_type": "remote_branch_cleanup",
                "kind": "remote_branch_cleanup_completed",
                "task_key": self.task_key,
                "task_status": task_status,
                "remote": "origin",
                "branch": self.branch,
                "remote_branch_deleted": True,
                "remote_branch_exists_before": True,
                "remote_branch_exists_after": False,
                "remote_branch_delete_attempted": True,
                "remote_branch_delete_error": None,
                "issue_closed": False,
                "task_status_changed": False,
                "task_completed": False,
                "task_archived": False,
                "cleanup_scope": "remote_branch",
                "requires_human_confirmation": True,
                "confirmation_flag": "--confirm-remote-branch-delete",
            }
            remote_path.write_text(json.dumps(remote_payload, indent=2, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "remote_branch_cleanup", remote_path)
            self.store.record_task_event(
                self.task_key,
                "remote_branch_cleanup_completed",
                "remote_branch_cleanup_confirm",
                payload=remote_payload,
            )

    def _run_main(self, argv: list[str], *, runner: FakeCloseoutRunner | None = None) -> tuple[int, str, str]:
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
            "--repo",
            "anderson930420/agent-taskflow",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
        ]

    def test_script_requires_task_key(self) -> None:
        exit_code, _stdout, stderr = self._run_main(
            [
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--json",
            ],
            runner=FakeCloseoutRunner(),
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("--task-key", stderr)

    def test_script_requires_confirm_flag_for_actual_closeout(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--json"], runner=FakeCloseoutRunner())

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-task-closeout", payload["error"])
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")

    def test_script_supports_dry_run_without_status_update(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--dry-run", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["task_status_changed"])
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")

    def test_script_prints_valid_json(self) -> None:
        self._seed_task()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-task-closeout", "--json"],
            runner=FakeCloseoutRunner(),
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "task_closeout_completed")

    def test_script_handles_missing_db_without_creating_state(self) -> None:
        missing_db = self.root / "missing.db"
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                self.task_key,
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(missing_db),
                "--json",
            ],
            runner=FakeCloseoutRunner(),
        )

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertIn(payload["status"], {"blocked", "not_found"})
        self.assertFalse(missing_db.exists())

    def test_script_blocks_missing_evidence(self) -> None:
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._seed_task(include_local_cleanup=False)
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-task-closeout", "--json"], runner=FakeCloseoutRunner())

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("Local cleanup", payload["error"])

    def test_script_does_not_close_issue_delete_branch_remove_worktree_or_merge(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-task-closeout", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "task_closeout_completed")
        command_prefixes = [call["args"][:3] for call in runner.calls]
        self.assertEqual(command_prefixes, [["gh", "pr", "view"], ["git", "ls-remote", "--heads"]])

    def test_script_writes_evidence_only_after_successful_status_update(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        exit_code, stdout, _stderr = self._run_main(self._base_args() + ["--confirm-task-closeout", "--json"], runner=runner)

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "task_closeout_completed")
        self.assertEqual(self.store.get_task(self.task_key).status, "completed")
        closeout_artifacts = [artifact for artifact in self.store.list_task_artifacts(self.task_key) if artifact.artifact_type == "task_closeout"]
        closeout_events = [event for event in self.store.list_task_events(self.task_key) if event.event_type == "task_closeout_completed"]
        self.assertEqual(len(closeout_artifacts), 1)
        self.assertEqual(len(closeout_events), 1)
