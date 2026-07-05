from __future__ import annotations

from dataclasses import dataclass
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_closeout_confirm import (
    TaskCloseoutConfirmRequest,
    confirm_task_closeout,
)


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeCloseoutRunner:
    def __init__(
        self,
        *,
        merged: bool = True,
        remote_exists: bool = False,
        pr_number: int = 22,
        pr_url: str = "https://github.com/anderson930420/agent-taskflow/pull/22",
        head_ref_name: str = "task/AT-CC-001",
        base_ref_name: str = "main",
    ) -> None:
        self.merged = merged
        self.remote_exists = remote_exists
        self.pr_number = pr_number
        self.pr_url = pr_url
        self.head_ref_name = head_ref_name
        self.base_ref_name = base_ref_name
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "cwd": kwargs.get("cwd")})

        if args[:3] == ["gh", "pr", "view"]:
            payload = {
                "number": self.pr_number,
                "url": self.pr_url,
                "state": "MERGED" if self.merged else "OPEN",
                "isDraft": False,
                "mergedAt": "2026-05-18T00:00:00Z" if self.merged else None,
                "mergeCommit": {"oid": "deadbeef"} if self.merged else None,
                "headRefName": self.head_ref_name,
                "baseRefName": self.base_ref_name,
                "title": "Task closeout candidate",
            }
            return FakeCompletedProcess(returncode=0, stdout=json.dumps(payload))

        if args[:3] == ["git", "ls-remote", "--heads"]:
            if self.remote_exists:
                return FakeCompletedProcess(
                    returncode=0,
                    stdout=f"deadbeef\trefs/heads/{self.head_ref_name}\n",
                )
            return FakeCompletedProcess(returncode=0, stdout="")

        raise AssertionError(f"unexpected command: {args}")


class TaskCloseoutConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-CC-001"
        self.branch = f"task/{self.task_key}"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        task_status: str = "waiting_approval",
        include_draft_pr: bool = True,
        include_local_cleanup: bool = True,
        include_remote_cleanup: bool = True,
        draft_overrides: dict[str, Any] | None = None,
        local_overrides: dict[str, Any] | None = None,
        remote_overrides: dict[str, Any] | None = None,
    ) -> None:
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Task closeout candidate",
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
            draft_payload: dict[str, Any] = {
                "schema_version": "1",
                "artifact_type": "draft_pr",
                "kind": "draft_pr_created",
                "task_key": self.task_key,
                "repo": "anderson930420/agent-taskflow",
                "base_branch": "main",
                "head_branch": self.branch,
                "title": "Task closeout candidate",
                "body": "Draft PR body",
                "draft": True,
                "pr_number": 22,
                "pr_url": "https://github.com/anderson930420/agent-taskflow/pull/22",
                "branch_push_verified": True,
                "verified": True,
                "pr_created": True,
                "draft_pr_created": True,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "issue_closed": False,
                "requires_human_confirmation": True,
            }
            if draft_overrides:
                draft_payload.update(draft_overrides)
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
            local_payload: dict[str, Any] = {
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
                "remote_branch_deleted": False,
                "issue_closed": False,
                "task_status_changed": False,
                "task_completed": False,
                "task_archived": False,
                "cleanup_scope": "local",
                "requires_human_confirmation": True,
                "confirmation_flag": "--confirm-local-cleanup",
            }
            if local_overrides:
                local_payload.update(local_overrides)
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
            remote_payload: dict[str, Any] = {
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
            if remote_overrides:
                remote_payload.update(remote_overrides)
            remote_path.write_text(json.dumps(remote_payload, indent=2, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "remote_branch_cleanup", remote_path)
            self.store.record_task_event(
                self.task_key,
                "remote_branch_cleanup_completed",
                "remote_branch_cleanup_confirm",
                payload=remote_payload,
            )

    def _request(
        self,
        *,
        dry_run: bool = False,
        confirm: bool = False,
        target_status: str = "completed",
    ) -> TaskCloseoutConfirmRequest:
        return TaskCloseoutConfirmRequest(
            task_key=self.task_key,
            repo="anderson930420/agent-taskflow",
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            target_status=target_status,
            dry_run=dry_run,
            confirm_task_closeout=confirm,
        )

    def test_missing_task_returns_not_found(self) -> None:
        result = confirm_task_closeout(
            TaskCloseoutConfirmRequest(
                task_key="AT-MISSING",
                repo="anderson930420/agent-taskflow",
                repo_path=self.repo,
                db_path=self.db_path,
            ),
            store=self.store,
            runner=FakeCloseoutRunner(),
        ).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertIn("Task not found", result["error"])

    def test_missing_confirm_refuses_status_update(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        result = confirm_task_closeout(self._request(confirm=False), store=self.store, runner=runner).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("--confirm-task-closeout", result["error"])
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), 3)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), 3)

    def test_dry_run_does_not_update_status_or_write_evidence(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        result = confirm_task_closeout(self._request(dry_run=True), store=self.store, runner=runner).to_dict()

        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["closeout_ready"])
        self.assertFalse(result["task_status_changed"])
        self.assertFalse(result["evidence"]["artifact_recorded"])
        self.assertFalse(result["evidence"]["event_recorded"])
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), 3)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), 3)

    def test_missing_or_unverified_draft_pr_evidence_blocks_closeout(self) -> None:
        cases = [
            ("missing", {"include_draft_pr": False}),
            ("unverified", {"draft_overrides": {"verified": False}}),
        ]
        for _label, kwargs in cases:
            with self.subTest(_label):
                self.tmp.cleanup()
                self.setUp()
                self._seed_task(**kwargs)
                result = confirm_task_closeout(self._request(), store=self.store, runner=FakeCloseoutRunner()).to_dict()
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked")
                self.assertIn("Draft PR", result["error"])

    def test_pr_not_merged_blocks_closeout(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner(merged=False)
        result = confirm_task_closeout(self._request(), store=self.store, runner=runner).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("GitHub PR is not merged", result["error"])

    def test_missing_or_inconsistent_cleanup_evidence_blocks_closeout(self) -> None:
        cases = [
            ("missing_local", {"include_local_cleanup": False}, "Local cleanup"),
            ("missing_remote", {"include_remote_cleanup": False}, "Remote branch cleanup"),
            ("remote_deleted_false", {"remote_overrides": {"remote_branch_deleted": False}}, "remote_branch_deleted"),
            ("local_issue_closed_true", {"local_overrides": {"issue_closed": True}}, "issue_closed"),
            ("remote_task_completed_true", {"remote_overrides": {"task_completed": True}}, "task_completed"),
        ]
        for label, kwargs, expected in cases:
            with self.subTest(label):
                self.tmp.cleanup()
                self.setUp()
                self._seed_task(**kwargs)
                result = confirm_task_closeout(self._request(), store=self.store, runner=FakeCloseoutRunner()).to_dict()
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked")
                self.assertIn(expected, result["error"])

    def test_successful_closeout_updates_status_and_records_evidence(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        result = confirm_task_closeout(self._request(confirm=True), store=self.store, runner=runner).to_dict()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "task_closeout_completed")
        self.assertEqual(result["previous_task_status"], "waiting_approval")
        self.assertEqual(result["new_task_status"], "completed")
        self.assertTrue(result["task_status_changed"])
        self.assertTrue(result["db_written"])
        self.assertTrue(result["task_closeout_performed"])
        self.assertEqual(self.store.get_task(self.task_key).status, "completed")

        artifacts = self.store.list_task_artifacts(self.task_key)
        events = self.store.list_task_events(self.task_key)
        closeout_artifacts = [artifact for artifact in artifacts if artifact.artifact_type == "task_closeout"]
        closeout_events = [event for event in events if event.event_type == "task_closeout_completed"]
        self.assertEqual(len(closeout_artifacts), 1)
        self.assertEqual(len(closeout_events), 1)

        payload = json.loads(Path(closeout_artifacts[0].path).read_text(encoding="utf-8"))
        self.assertFalse(payload["issue_closed"])
        self.assertFalse(payload["github_issue_mutated"])
        self.assertTrue(payload["task_status_changed"])
        self.assertTrue(payload["task_completed"])
        self.assertFalse(payload["task_archived"])

    def test_successful_closeout_is_idempotent_when_already_completed(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        first = confirm_task_closeout(self._request(confirm=True), store=self.store, runner=runner).to_dict()
        self.assertEqual(first["status"], "task_closeout_completed")

        second_runner = FakeCloseoutRunner()
        second = confirm_task_closeout(self._request(confirm=False), store=self.store, runner=second_runner).to_dict()

        self.assertTrue(second["ok"])
        self.assertEqual(second["status"], "already_completed")
        self.assertFalse(second["task_status_changed"])
        self.assertFalse(second["db_written"])
        self.assertEqual(len([artifact for artifact in self.store.list_task_artifacts(self.task_key) if artifact.artifact_type == "task_closeout"]), 1)
        self.assertEqual(len([event for event in self.store.list_task_events(self.task_key) if event.event_type == "task_closeout_completed"]), 1)

    def test_no_cleanup_helpers_are_called(self) -> None:
        self._seed_task()
        runner = FakeCloseoutRunner()
        confirm_task_closeout(self._request(confirm=True), store=self.store, runner=runner)

        command_prefixes = [call["args"][:3] for call in runner.calls]
        self.assertEqual(command_prefixes, [["gh", "pr", "view"], ["git", "ls-remote", "--heads"]])

    def test_closeout_artifact_preserves_formatting(self) -> None:
        self._seed_task()
        confirm_task_closeout(self._request(confirm=True), store=self.store, runner=FakeCloseoutRunner())

        artifact_path = self.artifact_root / "task_closeout" / self.task_key / "task_closeout.json"
        text = artifact_path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")

    def test_failed_closeout_artifact_write_preserves_existing_file(self) -> None:
        self._seed_task()
        artifact_path = self.artifact_root / "task_closeout" / self.task_key / "task_closeout.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        previous = '{"status": "previous-complete-artifact"}\n'
        artifact_path.write_text(previous, encoding="utf-8")

        real_replace = os.replace

        def failing_replace(src, dst, *args, **kwargs):
            if Path(dst).name == "task_closeout.json":
                raise OSError("simulated crash before replace")
            return real_replace(src, dst, *args, **kwargs)

        with patch("agent_taskflow.atomic_write.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                confirm_task_closeout(self._request(confirm=True), store=self.store, runner=FakeCloseoutRunner())

        self.assertEqual(artifact_path.read_text(encoding="utf-8"), previous)
        leftovers = [path for path in artifact_path.parent.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
