from __future__ import annotations

from dataclasses import dataclass
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.remote_branch_cleanup_confirm import (
    RemoteBranchCleanupConfirmRequest,
    confirm_remote_branch_cleanup,
)
from agent_taskflow.store import TaskMirrorStore


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeRemoteCleanupRunner:
    def __init__(
        self,
        *,
        branch_name: str = "task/AT-RC-001",
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


class RemoteBranchCleanupConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.repo / ".worktrees" / "AT-RC-001"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-RC-001"
        self.branch = f"task/{self.task_key}"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        branch: str | None = None,
        base_branch: str = "main",
        task_status: str = "waiting_approval",
        with_draft_pr: bool = True,
        with_local_cleanup: bool = True,
        merged_pr: bool = True,
        remote_exists: bool = True,
        branch_exists: bool = True,
        offline_pr_valid: bool = True,
        offline_pr_base_branch: str | None = None,
        local_cleanup_overrides: dict[str, Any] | None = None,
    ) -> None:
        branch_name = branch or self.branch
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Remote branch cleanup task",
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

        if with_draft_pr:
            draft_pr_path = artifact_dir / "draft_pr.json"
            draft_pr_payload = {
                "schema_version": "1",
                "artifact_type": "draft_pr",
                "kind": "draft_pr_created",
                "task_key": self.task_key,
                "repo": "anderson930420/agent-taskflow",
                "base_branch": base_branch,
                "head_branch": branch_name,
                "title": "Remote branch cleanup task",
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

        if with_local_cleanup:
            local_cleanup_dir = self.artifact_root / "local_cleanup" / self.task_key
            local_cleanup_dir.mkdir(parents=True, exist_ok=True)
            local_cleanup_path = local_cleanup_dir / "local_cleanup.json"
            local_cleanup_payload: dict[str, Any] = {
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
            if local_cleanup_overrides:
                local_cleanup_payload.update(local_cleanup_overrides)
            local_cleanup_path.write_text(json.dumps(local_cleanup_payload, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "local_cleanup", local_cleanup_path)
            self.store.record_task_event(
                self.task_key,
                "local_cleanup_completed",
                "local_cleanup_confirm",
                payload=local_cleanup_payload,
            )

        offline_pr_json = self.root / "offline-pr.json"
        if offline_pr_valid:
            offline_pr_payload = {
                "number": 123,
                "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                "state": "MERGED" if merged_pr else "OPEN",
                "isDraft": False,
                "mergedAt": "2026-05-18T00:00:00Z" if merged_pr else None,
                "mergeCommit": {"oid": "deadbeef"} if merged_pr else None,
                "headRefName": branch_name,
                "baseRefName": offline_pr_base_branch or base_branch,
                "title": "Remote branch cleanup task",
            }
            offline_pr_json.write_text(
                json.dumps(offline_pr_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        else:
            offline_pr_json.write_text("{not json", encoding="utf-8")
        self.offline_pr_json = offline_pr_json
        self.runner = FakeRemoteCleanupRunner(
            branch_name=branch_name,
            branch_exists=branch_exists,
            merged_into_base=True,
            remote_exists=remote_exists,
        )

    def _request(
        self,
        *,
        branch: str | None = None,
        dry_run: bool = False,
        confirm: bool = False,
        remote: str = "origin",
        repo_path: Path | None = None,
    ) -> RemoteBranchCleanupConfirmRequest:
        return RemoteBranchCleanupConfirmRequest(
            task_key=self.task_key,
            repo_path=repo_path or self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            remote=remote,
            branch=branch,
            offline_pr_json=self.offline_pr_json,
            dry_run=dry_run,
            confirm_remote_branch_delete=confirm,
        )

    def test_missing_task_returns_not_found(self) -> None:
        result = confirm_remote_branch_cleanup(
            RemoteBranchCleanupConfirmRequest(
                task_key="AT-MISSING",
                repo_path=self.repo,
                db_path=self.db_path,
            ),
            store=self.store,
            runner=FakeRemoteCleanupRunner(),
        ).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertIn("Task not found", result["error"])

    def test_missing_confirm_refuses_actual_deletion(self) -> None:
        self._seed_task()

        result = confirm_remote_branch_cleanup(
            self._request(),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("--confirm-remote-branch-delete", result["error"])
        self.assertFalse(any(call["args"][:3] == ["git", "push", "origin"] for call in self.runner.calls))

    def test_dry_run_does_not_delete_remote_branch(self) -> None:
        self._seed_task()

        result = confirm_remote_branch_cleanup(
            self._request(dry_run=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["performed"])
        self.assertFalse(result["remote_branch"]["deleted"])
        self.assertFalse(result["evidence"]["artifact_recorded"])
        self.assertFalse(result["evidence"]["event_recorded"])
        self.assertFalse(any(call["args"][:3] == ["git", "push", "origin"] for call in self.runner.calls))

    def test_missing_phase6a_cleanup_recommendation_blocks_deletion(self) -> None:
        self._seed_task(offline_pr_valid=False)

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["cleanup_recommendation"]["available"])
        self.assertFalse(result["remote_branch_cleanup_performed"])
        self.assertFalse(any(call["args"][:3] == ["git", "push", "origin"] for call in self.runner.calls))

    def test_pr_not_merged_blocks_deletion(self) -> None:
        self._seed_task(merged_pr=False)

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("not merged", " ".join(result["blocking_warnings"]).lower())
        self.assertFalse(result["remote_branch_cleanup_performed"])

    def test_missing_phase6b_local_cleanup_evidence_blocks_deletion(self) -> None:
        self._seed_task(with_local_cleanup=False)

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("local cleanup evidence", " ".join(result["blocking_warnings"]).lower())
        self.assertFalse(result["remote_branch_cleanup_performed"])

    def test_local_cleanup_evidence_with_task_completed_true_blocks_deletion(self) -> None:
        self._seed_task(local_cleanup_overrides={"task_completed": True})

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("task_completed", " ".join(result["blocking_warnings"]))
        self.assertFalse(result["remote_branch_cleanup_performed"])

    def test_local_cleanup_evidence_with_issue_closed_true_blocks_deletion(self) -> None:
        self._seed_task(local_cleanup_overrides={"issue_closed": True})

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("issue_closed", " ".join(result["blocking_warnings"]))
        self.assertFalse(result["remote_branch_cleanup_performed"])

    def test_remote_branch_missing_blocks_without_recording_completion(self) -> None:
        self._seed_task(remote_exists=False)

        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing", " ".join(result["blocking_warnings"]).lower())
        self.assertFalse(result["remote_branch_cleanup_performed"])
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), before_artifacts)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), before_events)

    def test_main_branch_blocks_deletion(self) -> None:
        self._seed_task(branch="main")

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=FakeRemoteCleanupRunner(branch_name="main"),
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("protected", " ".join(result["blocking_warnings"]).lower())

    def test_base_branch_blocks_deletion(self) -> None:
        self._seed_task(base_branch=self.branch, offline_pr_base_branch=self.branch)

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("base branch", " ".join(result["blocking_warnings"]).lower())

    def test_invalid_branch_name_blocks_deletion(self) -> None:
        self._seed_task()

        result = confirm_remote_branch_cleanup(
            self._request(branch="bad branch", confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("branch", result["error"].lower())
        self.assertFalse(result["remote_branch_cleanup_performed"])

    def test_branch_override_must_match_trusted_evidence(self) -> None:
        self._seed_task()

        result = confirm_remote_branch_cleanup(
            self._request(branch="task/AT-OTHER-001", confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("trusted task branch evidence", result["error"])
        self.assertFalse(result["remote_branch_cleanup_performed"])

    def test_successful_cleanup_uses_git_push_delete(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "remote_branch_cleanup_completed")
        self.assertTrue(result["remote_branch"]["deleted"])
        self.assertIn(["git", "push", "origin", "--delete", self.branch], [call["args"] for call in self.runner.calls])
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), before_artifacts + 1)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), before_events + 1)
        self.assertTrue(result["evidence"]["artifact_recorded"])
        self.assertTrue(result["evidence"]["event_recorded"])

    def test_failed_remote_deletion_does_not_record_completion(self) -> None:
        self._seed_task()
        self.runner.push_returncode = 1
        self.runner.push_stderr = "permission denied"
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("failed", result["error"].lower())
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), before_artifacts)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), before_events)
        self.assertFalse(result["evidence"]["artifact_recorded"])
        self.assertFalse(result["evidence"]["event_recorded"])

    def test_evidence_is_recorded_only_after_actual_remote_deletion(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))

        result = confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertEqual(result["status"], "remote_branch_cleanup_completed")
        self.assertEqual(len(self.store.list_task_artifacts(self.task_key)), before_artifacts + 1)
        self.assertEqual(len(self.store.list_task_events(self.task_key)), before_events + 1)

    def test_safety_block_says_false_fields(self) -> None:
        self._seed_task()

        result = confirm_remote_branch_cleanup(
            self._request(),
            store=self.store,
            runner=self.runner,
        ).to_dict()

        self.assertFalse(result["safety"]["issue_closed"])
        self.assertFalse(result["safety"]["task_status_changed"])
        self.assertFalse(result["safety"]["task_archived"])
        self.assertFalse(result["safety"]["task_completed"])

    def test_no_local_branch_delete_or_worktree_remove_helpers_are_called(self) -> None:
        self._seed_task()

        confirm_remote_branch_cleanup(
            self._request(confirm=True),
            store=self.store,
            runner=self.runner,
        )

        commands = [" ".join(call["args"]) for call in self.runner.calls]
        self.assertFalse(any("git branch -d" in command for command in commands))
        self.assertFalse(any("git worktree remove" in command for command in commands))
        self.assertFalse(any("gh issue close" in command for command in commands))
        self.assertFalse(any("gh pr merge" in command for command in commands))
        self.assertFalse(any("gh pr review" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
