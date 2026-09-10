"""Tests for agent_taskflow.integration_cleanup (spec §36, §37, §37.1, §44)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_schema as schema
from agent_taskflow import integration_git as git_ops
from agent_taskflow.integration_cleanup import (
    IntegrationCleanupRequest,
    run_integration_cleanup,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from v1_step2_fixtures import GitFixture, git as raw_git  # noqa: E402


class CleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.worktree = self._make_task("AT-301")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_task(self, task_key: str) -> Path:
        worktree = self.fixture.create_task_worktree(task_key)
        self.fixture.commit_in(worktree, f"{task_key}.txt", "f\n", "feature")
        git_ops.push_branch(
            worktree, remote="origin", branch=f"task/{task_key}", base_branch="main"
        )
        artifact_dir = self.artifacts / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "evidence.json").write_text("{}", encoding="utf-8")
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="demo",
                status=schema.NEEDS_REVIEW,
                repo_path=self.fixture.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.fixture.repo,
                worktree_path=worktree,
                branch=f"task/{task_key}",
                base_branch="main",
                base_sha=self.fixture.target_sha(),
                status="active",
            )
        )
        self.integration.update_pr_state(
            task_key,
            pr_number=42,
            pr_url="https://github.com/owner/repo/pull/42",
            pr_state="open",
        )
        return worktree

    def _merge(self, task_key: str = "AT-301", *, method: str = "merge") -> str:
        merge_sha = self.fixture.merge_branch_into_target(f"task/{task_key}", method=method)
        self.integration.update_pr_state(
            task_key,
            pr_state="closed",
            pr_merged=True,
            merge_commit_sha=merge_sha,
        )
        return merge_sha

    def request(self, task_key: str = "AT-301", **overrides) -> IntegrationCleanupRequest:
        kwargs = dict(
            task_key=task_key,
            repo="owner/repo",
            repo_path=self.fixture.repo,
            target_branch="main",
            remote="origin",
            db_path=self.db_path,
            confirm_cleanup=True,
        )
        kwargs.update(overrides)
        return IntegrationCleanupRequest(**kwargs)

    def cleanup(self, task_key: str = "AT-301", **overrides):
        return run_integration_cleanup(
            self.request(task_key, **overrides),
            store=self.store,
            integration_store=self.integration,
        )

    def status_of(self, task_key: str) -> str:
        return self.store.get_task(task_key).status


class MergeVerifiedCleanupTests(CleanupTestCase):
    def test_cleanup_after_a_verified_merge_completes_the_ticket(self) -> None:
        self._merge()
        result = self.cleanup()
        self.assertTrue(result.ok, result.summary)
        self.assertTrue(result.merge_verified)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.status_of("AT-301"), schema.COMPLETED)

    def test_cleanup_supports_squash_and_rebase_merges(self) -> None:
        for method in ("squash", "rebase"):
            with self.subTest(method=method):
                task_key = f"AT-30{method[0]}"
                worktree = self._make_task(task_key)
                self._merge(task_key, method=method)
                result = self.cleanup(task_key)
                self.assertTrue(result.ok, result.summary)
                self.assertFalse(worktree.exists())
                self.assertEqual(self.status_of(task_key), schema.COMPLETED)

    def test_the_local_branch_is_safe_deleted(self) -> None:
        self._merge()
        self.cleanup()
        self.assertEqual(raw_git(self.fixture.repo, "branch", "--list", "task/AT-301").strip(), "")

    def test_evidence_is_archived_not_destroyed(self) -> None:
        self._merge()
        result = self.cleanup()
        self.assertTrue(result.evidence_archived)
        self.assertTrue((self.artifacts / "AT-301" / "evidence.json").is_file())

    def test_remote_branch_cleanup_is_off_by_default(self) -> None:
        self._merge()
        result = self.cleanup()
        self.assertIs(result.remote_branch_deleted, False)
        self.assertIn(
            "task/AT-301", raw_git(self.fixture.origin, "branch", "--list", "task/AT-301")
        )

    def test_remote_branch_cleanup_runs_only_when_requested(self) -> None:
        self._merge()
        result = self.cleanup(delete_remote_branch=True)
        self.assertIs(result.remote_branch_deleted, True)
        self.assertEqual(
            raw_git(self.fixture.origin, "branch", "--list", "task/AT-301").strip(), ""
        )


class CleanupGateTests(CleanupTestCase):
    def test_cleanup_is_refused_when_the_pr_is_not_merged(self) -> None:
        result = self.cleanup()
        self.assertFalse(result.ok)
        self.assertFalse(result.merge_verified)
        self.assertTrue(self.worktree.is_dir())
        self.assertNotEqual(self.status_of("AT-301"), schema.COMPLETED)

    def test_cleanup_is_refused_when_the_merge_sha_is_not_in_target_history(self) -> None:
        self.integration.update_pr_state(
            "AT-301", pr_merged=True, merge_commit_sha=git_ops.head_sha(self.worktree)
        )
        result = self.cleanup()
        self.assertFalse(result.ok)
        self.assertTrue(self.worktree.is_dir())

    def test_cleanup_requires_explicit_confirmation(self) -> None:
        self._merge()
        result = self.cleanup(confirm_cleanup=False)
        self.assertEqual(result.status, "dry_run")
        self.assertTrue(self.worktree.is_dir())
        self.assertNotEqual(self.status_of("AT-301"), schema.COMPLETED)

    def test_completed_is_never_reached_without_a_verified_merge(self) -> None:
        for setup in (
            lambda: None,
            lambda: self.integration.update_pr_state("AT-301", pr_merged=True),
            lambda: self.integration.update_pr_state("AT-301", merge_commit_sha="0" * 40),
        ):
            setup()
            self.cleanup()
            self.assertNotEqual(self.status_of("AT-301"), schema.COMPLETED)


class ClosedUnmergedTests(CleanupTestCase):
    def _cancel(self) -> None:
        self.integration.update_pr_state("AT-301", pr_state="closed", pr_merged=False)
        self.store.update_task_status("AT-301", schema.CANCELLED, source="test")

    def test_cancelled_work_is_not_destroyed_by_the_merged_path(self) -> None:
        self._cancel()
        result = self.cleanup()
        self.assertFalse(result.ok)
        self.assertTrue(self.worktree.is_dir())
        self.assertIs(result.worktree_removed, False)

    def test_cancelled_cleanup_needs_its_own_confirmation_flag(self) -> None:
        self._cancel()
        result = self.cleanup(confirm_cleanup=True, confirm_cancelled_cleanup=False)
        self.assertFalse(result.ok)
        self.assertTrue(self.worktree.is_dir())
        self.assertIn("confirm", result.summary.lower())

    def test_cancelled_cleanup_runs_with_the_explicit_flag(self) -> None:
        self._cancel()
        result = self.cleanup(confirm_cleanup=True, confirm_cancelled_cleanup=True)
        self.assertTrue(result.ok, result.summary)
        self.assertFalse(self.worktree.exists())

    def test_cancelled_cleanup_never_marks_the_ticket_completed(self) -> None:
        self._cancel()
        self.cleanup(confirm_cleanup=True, confirm_cancelled_cleanup=True)
        self.assertEqual(self.status_of("AT-301"), schema.CANCELLED)

    def test_the_cancelled_flag_does_not_bypass_the_merge_gate_for_open_prs(self) -> None:
        result = self.cleanup(confirm_cleanup=True, confirm_cancelled_cleanup=True)
        self.assertFalse(result.ok)
        self.assertTrue(self.worktree.is_dir())

    def test_cancelled_cleanup_retains_archived_evidence(self) -> None:
        self._cancel()
        self.cleanup(confirm_cleanup=True, confirm_cancelled_cleanup=True)
        self.assertTrue((self.artifacts / "AT-301" / "evidence.json").is_file())


class NeverMergesTests(CleanupTestCase):
    def test_cleanup_never_invokes_a_merge_command(self) -> None:
        self._merge()
        result = self.cleanup()
        for command in result.git_commands:
            self.assertNotIn("merge", command[:2])
            self.assertNotEqual(command[:2], ("gh", "pr"))

    def test_cleanup_never_force_pushes(self) -> None:
        self._merge()
        result = self.cleanup(delete_remote_branch=True)
        for command in result.git_commands:
            self.assertFalse({"--force", "-f", "--force-with-lease"} & set(command))


if __name__ == "__main__":
    unittest.main()
