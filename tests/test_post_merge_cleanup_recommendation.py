from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.post_merge_cleanup_recommendation import (
    PostMergeCleanupRecommendationError,
    PostMergeCleanupRecommendationRequest,
    recommend_post_merge_cleanup,
)
from agent_taskflow.store import TaskMirrorStore


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeInspectorRunner:
    def __init__(
        self,
        *,
        pr_payload: dict[str, Any] | None = None,
        pr_returncode: int = 0,
        pr_stdout: str | None = None,
        pr_stderr: str = "",
        status_stdout: str = "",
        status_returncode: int = 0,
        branch_exists: bool = True,
        merged_into_base: bool = True,
        remote_exists: bool = True,
        remote_returncode: int = 0,
        remote_stderr: str = "",
    ) -> None:
        self.pr_payload = pr_payload or {
            "number": 123,
            "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
            "state": "MERGED",
            "isDraft": False,
            "mergedAt": "2026-05-18T00:00:00Z",
            "mergeCommit": {"oid": "deadbeef"},
            "headRefName": "task/AT-POST-MERGE-001",
            "baseRefName": "main",
            "title": "Post merge cleanup task",
        }
        self.pr_returncode = pr_returncode
        self.pr_stdout = pr_stdout
        self.pr_stderr = pr_stderr
        self.status_stdout = status_stdout
        self.status_returncode = status_returncode
        self.branch_exists = branch_exists
        self.merged_into_base = merged_into_base
        self.remote_exists = remote_exists
        self.remote_returncode = remote_returncode
        self.remote_stderr = remote_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if args[:3] == ["gh", "pr", "view"]:
            stdout = self.pr_stdout if self.pr_stdout is not None else json.dumps(self.pr_payload)
            return FakeCompletedProcess(
                returncode=self.pr_returncode,
                stdout=stdout,
                stderr=self.pr_stderr,
            )
        if args[:2] == ["git", "status"]:
            return FakeCompletedProcess(
                returncode=self.status_returncode,
                stdout=self.status_stdout,
                stderr="",
            )
        if args[:2] == ["git", "branch"] and "--list" in args:
            stdout = "task/AT-POST-MERGE-001\n" if self.branch_exists else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout, stderr="")
        if args[:2] == ["git", "branch"] and "--merged" in args:
            stdout = "task/AT-POST-MERGE-001\n" if self.merged_into_base else ""
            return FakeCompletedProcess(returncode=0, stdout=stdout, stderr="")
        if args[:2] == ["git", "ls-remote"] and "--heads" in args:
            stdout = "deadbeef\trefs/heads/task/AT-POST-MERGE-001\n" if self.remote_exists else ""
            return FakeCompletedProcess(
                returncode=self.remote_returncode,
                stdout=stdout,
                stderr=self.remote_stderr,
            )
        raise AssertionError(f"unexpected command: {args}")


class PostMergeCleanupRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "worktree"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-POST-MERGE-001"
        self.branch = f"task/{self.task_key}"
        self.base_sha = "base-sha"
        self._seed_task(with_worktree=True, worktree_exists=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        with_worktree: bool,
        worktree_exists: bool,
        status: str = "waiting_approval",
        branch: str | None = None,
        base_branch: str = "main",
        base_sha: str = "base-sha",
        with_draft_pr: bool = True,
        draft_pr_repo: str = "anderson930420/agent-taskflow",
        draft_pr_number: int = 123,
        draft_pr_url: str = "https://github.com/anderson930420/agent-taskflow/pull/123",
        event_kind: str = "draft_pr_created",
    ) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Post merge cleanup task",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        if with_worktree:
            worktree_path = self.worktree
            if worktree_exists:
                worktree_path.mkdir(parents=True, exist_ok=True)
                (worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
            self.store.upsert_task_worktree(
                TaskWorktreeRecord(
                    task_key=self.task_key,
                    repo_path=self.repo,
                    worktree_path=worktree_path,
                    branch=branch or self.branch,
                    base_branch=base_branch,
                    base_sha=base_sha,
                    status="active",
                )
            )
        if with_draft_pr:
            draft_pr_path = artifact_dir / "draft_pr.json"
            draft_pr_payload = {
                "kind": event_kind,
                "artifact_type": "draft_pr",
                "task_key": self.task_key,
                "repo": draft_pr_repo,
                "base_branch": base_branch,
                "head_branch": branch or self.branch,
                "title": "Post merge cleanup task",
                "body": "Draft PR body",
                "draft": True,
                "pr_number": draft_pr_number,
                "pr_url": draft_pr_url,
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

    def _request(
        self,
        *,
        pr_number: int | None = None,
        pr_url: str | None = None,
        allow_non_waiting: bool = False,
        offline_pr_json: Path | None = None,
        remote: str = "origin",
    ) -> PostMergeCleanupRecommendationRequest:
        return PostMergeCleanupRecommendationRequest(
            task_key=self.task_key,
            repo="anderson930420/agent-taskflow",
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            remote=remote,
            pr_number=pr_number,
            pr_url=pr_url,
            offline_pr_json=offline_pr_json,
            allow_non_waiting=allow_non_waiting,
        )

    def _recommend(
        self,
        *,
        runner: FakeInspectorRunner | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        result = recommend_post_merge_cleanup(
            self._request(**overrides),
            store=self.store,
            runner=runner,
        )
        return result.to_dict()

    def test_missing_task_returns_not_found(self) -> None:
        result = recommend_post_merge_cleanup(
            PostMergeCleanupRecommendationRequest(
                task_key="AT-MISSING",
                repo="anderson930420/agent-taskflow",
                repo_path=self.repo,
                db_path=self.db_path,
            ),
            store=self.store,
            runner=FakeInspectorRunner(),
        ).to_dict()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertIn("Task not found", result["summary"]["reason"])

    def test_missing_draft_pr_evidence_blocks_cleanup_recommendation(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM task_events WHERE task_key = ? AND event_type = ?",
                (self.task_key, "draft_pr_created"),
            )
            conn.execute(
                "DELETE FROM task_artifacts WHERE task_key = ? AND artifact_type = ?",
                (self.task_key, "draft_pr"),
            )
        draft_pr_path = self.artifact_root / self.task_key / "draft_pr.json"
        if draft_pr_path.exists():
            draft_pr_path.unlink()

        result = self._recommend(runner=FakeInspectorRunner())

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["recommended_cleanup"], [])
        self.assertIn("Draft PR evidence is missing", result["summary"]["reason"])

    def test_pr_not_merged_returns_no_cleanup_recommendation(self) -> None:
        runner = FakeInspectorRunner(
            pr_payload={
                "number": 123,
                "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                "state": "OPEN",
                "isDraft": False,
                "mergedAt": None,
                "mergeCommit": None,
                "headRefName": self.branch,
                "baseRefName": "main",
                "title": "Post merge cleanup task",
            }
        )

        result = self._recommend(runner=runner)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "not_merged")
        self.assertEqual(result["recommended_cleanup"], [])
        self.assertFalse(result["summary"]["cleanup_recommended"])
        self.assertIn("PR is not merged", result["summary"]["reason"])

    def test_pr_merged_returns_cleanup_recommendations(self) -> None:
        runner = FakeInspectorRunner()

        result = self._recommend(runner=runner)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "merged_recommend_cleanup")
        self.assertTrue(result["summary"]["merged"])
        self.assertTrue(result["summary"]["cleanup_recommended"])
        self.assertGreaterEqual(len(result["recommended_cleanup"]), 4)
        self.assertTrue(result["workspace"]["exists"])
        self.assertTrue(result["local_branch"]["exists"])
        self.assertTrue(result["remote_branch"]["exists"])

    def test_missing_merge_commit_but_merged_at_present_is_handled_conservatively(self) -> None:
        runner = FakeInspectorRunner(
            pr_payload={
                "number": 123,
                "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                "state": "MERGED",
                "isDraft": False,
                "mergedAt": "2026-05-18T00:00:00Z",
                "mergeCommit": None,
                "headRefName": self.branch,
                "baseRefName": "main",
                "title": "Post merge cleanup task",
            }
        )

        result = self._recommend(runner=runner)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "merged_recommend_cleanup")
        self.assertIsNone(result["pr"]["merge_commit"])
        self.assertIn(
            "mergeCommit is unavailable",
            " ".join(result["non_blocking_warnings"]),
        )

    def test_unable_to_check_pr_status_blocks_cleanup_recommendation(self) -> None:
        runner = FakeInspectorRunner(pr_returncode=1, pr_stderr="gh unavailable")

        result = self._recommend(runner=runner)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["recommended_cleanup"], [])
        self.assertIn("gh pr view failed", result["summary"]["reason"])

    def test_existing_worktree_path_is_reported(self) -> None:
        result = self._recommend(runner=FakeInspectorRunner())

        self.assertTrue(result["workspace"]["exists"])
        self.assertEqual(result["workspace"]["worktree_path"], str(self.worktree))

    def test_missing_worktree_path_creates_warning(self) -> None:
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "missing-worktree"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._seed_task(with_worktree=True, worktree_exists=False)

        result = self._recommend(runner=FakeInspectorRunner())

        self.assertFalse(result["workspace"]["exists"])
        self.assertIn("Worktree path is missing", " ".join(result["non_blocking_warnings"]))
        self.assertEqual(
            result["recommended_cleanup"][0]["action"],
            "remove_local_worktree",
        )
        self.assertFalse(result["recommended_cleanup"][0]["recommended"])

    def test_uncommitted_worktree_changes_block_or_downgrade_worktree_removal(self) -> None:
        runner = FakeInspectorRunner(status_stdout=" M feature.txt\n")

        result = self._recommend(runner=runner)

        worktree_item = next(
            item for item in result["recommended_cleanup"] if item["action"] == "remove_local_worktree"
        )
        self.assertFalse(worktree_item["recommended"])
        self.assertGreater(len(worktree_item["blockers"]), 0)
        self.assertIn("uncommitted changes", worktree_item["reason"])

    def test_local_branch_exists_is_reported(self) -> None:
        result = self._recommend(runner=FakeInspectorRunner())

        self.assertTrue(result["local_branch"]["exists"])
        self.assertEqual(result["local_branch"]["name"], self.branch)

    def test_local_branch_not_merged_into_base_blocks_or_downgrades_branch_deletion(self) -> None:
        runner = FakeInspectorRunner(merged_into_base=False)

        result = self._recommend(runner=runner)

        branch_item = next(
            item for item in result["recommended_cleanup"] if item["action"] == "delete_local_branch"
        )
        self.assertFalse(branch_item["recommended"])
        self.assertEqual(branch_item["risk_level"], "high")
        self.assertIn("not merged", branch_item["reason"])

    def test_remote_branch_exists_is_reported(self) -> None:
        result = self._recommend(runner=FakeInspectorRunner())

        self.assertTrue(result["remote_branch"]["exists"])
        self.assertEqual(result["remote_branch"]["remote"], "origin")

    def test_remote_branch_deletion_is_high_risk_and_requires_confirmation(self) -> None:
        result = self._recommend(runner=FakeInspectorRunner())

        remote_item = next(
            item for item in result["recommended_cleanup"] if item["action"] == "delete_remote_branch"
        )
        self.assertTrue(remote_item["recommended"])
        self.assertEqual(remote_item["risk_level"], "high")
        self.assertTrue(remote_item["requires_human_confirmation"])

    def test_all_recommendations_have_performed_false(self) -> None:
        result = self._recommend(runner=FakeInspectorRunner())

        self.assertTrue(all(not item["performed"] for item in result["recommended_cleanup"]))

    def test_safety_block_says_cleanup_performed_false_and_no_mutations(self) -> None:
        result = self._recommend(runner=FakeInspectorRunner())

        safety = result["safety"]
        self.assertTrue(safety["read_only"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["local_branch_deleted"])
        self.assertFalse(safety["remote_branch_deleted"])
        self.assertFalse(safety["worktree_removed"])

    def test_no_cleanup_helpers_are_called(self) -> None:
        runner = FakeInspectorRunner()

        result = self._recommend(runner=runner)
        self.assertTrue(result["ok"])

        command_heads = [tuple(call["args"][:3]) for call in runner.calls]
        forbidden = {
            ("gh", "pr", "merge"),
            ("gh", "pr", "review"),
            ("gh", "pr", "ready"),
            ("gh", "pr", "close"),
            ("gh", "issue", "close"),
            ("git", "push", "--dry-run"),
            ("git", "push", "--set-upstream"),
            ("git", "branch", "-d"),
            ("git", "branch", "-D"),
            ("git", "worktree", "remove"),
            ("git", "clean", "-fd"),
            ("git", "reset", "--hard"),
            ("git", "merge", "--no-ff"),
        }
        for prefix in forbidden:
            self.assertNotIn(prefix, command_heads)


if __name__ == "__main__":
    unittest.main()
