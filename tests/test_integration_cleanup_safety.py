"""V1-F10 / OR-3: cleanup fails closed on unregistered or mismatched targets.

Once the integration tick runs cleanup unattended, nothing may remove a path
git has not registered as this Ticket's worktree, delete unpublished commits,
or complete a Ticket whose cleanup is incomplete.
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_integration_cleanup import CleanupTestCase  # noqa: E402
from v1_step2_fixtures import git as raw_git  # noqa: E402

from agent_taskflow import integration_cleanup  # noqa: E402
from agent_taskflow import integration_schema as schema  # noqa: E402
from agent_taskflow.models import TaskWorktreeRecord  # noqa: E402


class CleanupTargetSafetyTests(CleanupTestCase):
    def assert_untouched(self, result, *, status="cleanup_refused") -> None:
        self.assertFalse(result.ok, result.summary)
        self.assertEqual(result.status, status, result.summary)
        self.assertEqual(self.status_of("AT-301"), schema.NEEDS_REVIEW)
        state = self.integration.get_integration_state("AT-301")
        self.assertIsNone(state["cleanup_confirmed_at"])
        self.assertIn("task/AT-301", raw_git(self.fixture.repo, "branch", "--list", "task/AT-301"))

    def candidates(self) -> list[str]:
        return [s["task_key"] for s in self.integration.list_merged_unverified_pr_states("owner/repo")]

    def test_dirty_worktree_is_refused_before_any_write_and_stays_a_candidate(self) -> None:
        self._merge()
        sentinel = self.worktree / "do-not-delete.txt"
        sentinel.write_text("operator data\n", encoding="utf-8")
        result = self.cleanup()
        self.assert_untouched(result)
        self.assertIn("uncommitted or untracked", result.summary)
        self.assertTrue(sentinel.is_file())
        self.assertIsNone(self.integration.get_integration_state("AT-301")["merge_verified_at"])
        self.assertEqual(self.candidates(), ["AT-301"])
        self.assertFalse(any(c[:2] == ("git", "worktree") for c in result.git_commands))
        sentinel.unlink()
        self.assertTrue(self.cleanup().ok)
        self.assertEqual(self.status_of("AT-301"), schema.COMPLETED)

    def test_an_unregistered_directory_at_the_recorded_path_is_never_deleted(self) -> None:
        self._merge()
        raw_git(self.fixture.repo, "worktree", "remove", str(self.worktree))
        self.worktree.mkdir()
        keep = self.worktree / "someone-elses-file.txt"
        keep.write_text("not a worktree\n", encoding="utf-8")
        result = self.cleanup()
        self.assert_untouched(result)
        self.assertIn("not a git worktree", result.summary)
        self.assertTrue(keep.is_file())

    def test_a_worktree_on_another_branch_is_refused(self) -> None:
        self._merge()
        raw_git(self.worktree, "switch", "-c", "operator/other")
        result = self.cleanup()
        self.assert_untouched(result)
        self.assertIn("refs/heads/operator/other", result.summary)
        self.assertTrue(self.worktree.is_dir())

    def test_a_record_outside_the_repository_worktrees_is_refused(self) -> None:
        self._merge()
        # Inside the main checkout, so the merge still verifies from there.
        elsewhere = self.fixture.repo / "notes"
        elsewhere.mkdir()
        (elsewhere / "file.txt").write_text("x\n", encoding="utf-8")
        record = self.store.get_task_worktree("AT-301")
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key="AT-301", repo_path=record.repo_path, worktree_path=elsewhere,
            branch=record.branch, base_branch=record.base_branch, base_sha=record.base_sha,
            status="active", created_at=record.created_at,
        ))
        result = self.cleanup()
        self.assert_untouched(result)
        self.assertTrue((elsewhere / "file.txt").is_file())
        self.assertTrue(self.worktree.is_dir())

    def test_a_record_for_another_repository_is_refused(self) -> None:
        self._merge()
        other = self.root / "other-repo"
        other.mkdir()
        record = self.store.get_task_worktree("AT-301")
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key="AT-301", repo_path=other, worktree_path=record.worktree_path,
            branch=record.branch, base_branch=record.base_branch, base_sha=record.base_sha,
            status="active", created_at=record.created_at,
        ))
        self.assert_untouched(self.cleanup())
        self.assertTrue(self.worktree.is_dir())

    def test_a_new_local_commit_after_the_merge_is_retained(self) -> None:
        self._merge()
        self.fixture.commit_in(self.worktree, "operator-followup.txt", "retain\n", "follow-up")
        result = self.cleanup()
        self.assert_untouched(result)
        self.assertIn("neither the recorded PR head nor the remote task branch", result.summary)
        self.assertTrue((self.worktree / "operator-followup.txt").is_file())

    def test_the_recorded_pr_head_suffices_when_the_remote_ref_is_gone(self) -> None:
        self._merge()
        tip = raw_git(self.fixture.repo, "rev-parse", "refs/heads/task/AT-301").strip()
        # GitHub deleted the head branch and the remote-tracking ref was pruned.
        raw_git(self.fixture.origin, "branch", "-D", "task/AT-301")
        raw_git(self.fixture.repo, "branch", "-dr", "origin/task/AT-301")
        self.assert_untouched(self.cleanup())
        self.integration.update_pr_state("AT-301", pr_head_sha=tip)
        result = self.cleanup()
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.status_of("AT-301"), schema.COMPLETED)

    def test_the_cancelled_route_also_fails_closed(self) -> None:
        self.integration.update_pr_state("AT-301", pr_state="closed", pr_merged=False)
        self.store.update_task_status("AT-301", schema.CANCELLED, source="test")
        (self.worktree / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        result = self.cleanup(confirm_cleanup=True, confirm_cancelled_cleanup=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cleanup_refused")
        self.assertTrue((self.worktree / "wip.txt").is_file())
        self.assertEqual(self.status_of("AT-301"), schema.CANCELLED)


class IncompleteCleanupTests(CleanupTestCase):
    def test_a_worktree_git_declines_to_remove_is_retained_and_never_completed(self) -> None:
        self._merge()
        raw_git(self.fixture.repo, "worktree", "lock", str(self.worktree))
        result = self.cleanup()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cleanup_incomplete")
        self.assertFalse(result.worktree_removed)
        self.assertFalse(result.local_branch_deleted)
        self.assertTrue(self.worktree.is_dir())
        self.assertTrue((self.worktree / "AT-301.txt").is_file())
        self.assertEqual(self.status_of("AT-301"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_integration_state("AT-301")["cleanup_confirmed_at"])
        # A human-confirmed retry after the cause is fixed finishes the job.
        raw_git(self.fixture.repo, "worktree", "unlock", str(self.worktree))
        retry = self.cleanup()
        self.assertTrue(retry.ok, retry.summary)
        self.assertEqual(self.status_of("AT-301"), schema.COMPLETED)

    def test_an_already_removed_worktree_is_idempotent_progress(self) -> None:
        self._merge()
        raw_git(self.fixture.repo, "worktree", "remove", str(self.worktree))
        result = self.cleanup()
        self.assertTrue(result.ok, result.summary)
        self.assertTrue(result.worktree_removed)
        self.assertTrue(result.local_branch_deleted)
        self.assertEqual(self.status_of("AT-301"), schema.COMPLETED)
        self.assertEqual(self.store.get_task_worktree("AT-301").status, "cleaned")

    def test_a_missing_directory_git_still_registers_is_refused(self) -> None:
        self._merge()
        shutil.rmtree(self.worktree)
        result = self.cleanup()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cleanup_refused")
        self.assertIn("registered with git but its directory is missing", result.summary)
        self.assertEqual(self.status_of("AT-301"), schema.NEEDS_REVIEW)


class BranchMovedDuringCleanupTests(CleanupTestCase):
    """Review N1: the tip is re-read after the target check, before `branch -D`."""

    def tip(self) -> str:
        return raw_git(self.fixture.repo, "rev-parse", "refs/heads/task/AT-301").strip()

    def race(self, move):
        """Clean up with ``move`` run after the target check, before removal."""
        remove = integration_cleanup._remove_worktree

        def racing(request, worktree, *, log):
            move()
            return remove(request, worktree, log=log)

        with patch.object(integration_cleanup, "_remove_worktree", racing):
            return self.cleanup()

    def test_a_commit_made_during_cleanup_keeps_the_branch_and_the_ticket(self) -> None:
        self._merge()
        checked = self.tip()
        moved = []
        result = self.race(lambda: moved.append(
            self.fixture.commit_in(self.worktree, "race.txt", "race\n", "racing commit")))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cleanup_incomplete", result.summary)
        self.assertTrue(result.worktree_removed)
        self.assertFalse(result.local_branch_deleted)
        self.assertIn(f"moved to {moved[0]} after the target check verified {checked}",
                      result.summary)
        self.assertNotIn(("git", "branch", "-D", "task/AT-301"), result.git_commands)
        # The racing commit is still on the branch: nothing was lost.
        self.assertEqual(self.tip(), moved[0])
        self.assertEqual(self.status_of("AT-301"), schema.NEEDS_REVIEW)
        state = self.integration.get_integration_state("AT-301")
        self.assertIsNone(state["cleanup_confirmed_at"])
        # Step 2's write order is unchanged (F10-FU3): the merge stays verified.
        self.assertIsNotNone(state["merge_verified_at"])
        # A retry checks the new tip, which is unpublished, and keeps it.
        retry = self.cleanup()
        self.assertEqual(retry.status, "cleanup_refused", retry.summary)
        self.assertIn("neither the recorded PR head nor the remote task branch", retry.summary)
        self.assertEqual(self.tip(), moved[0])
        self.assertEqual(self.status_of("AT-301"), schema.NEEDS_REVIEW)

    def test_a_branch_recreated_after_the_check_is_retained(self) -> None:
        self._merge()
        raw_git(self.fixture.repo, "worktree", "remove", str(self.worktree))
        raw_git(self.fixture.repo, "branch", "-D", "task/AT-301")
        # The check saw no branch at all, so any branch found later is new.
        result = self.race(lambda: raw_git(
            self.fixture.repo, "branch", "task/AT-301", "origin/task/AT-301"))
        self.assertEqual(result.status, "cleanup_incomplete", result.summary)
        self.assertIn("after the target check verified no branch", result.summary)
        self.assertTrue(self.tip())
        self.assertEqual(self.status_of("AT-301"), schema.NEEDS_REVIEW)
        # Its tip is published, so a human-confirmed retry finishes the job.
        retry = self.cleanup()
        self.assertTrue(retry.ok, retry.summary)
        self.assertEqual(self.status_of("AT-301"), schema.COMPLETED)


class PreviewPersistsNothingTests(CleanupTestCase):
    def test_an_unconfirmed_refusal_writes_no_artifact_or_row(self) -> None:
        before = sorted((self.artifacts / "AT-301").rglob("*"))
        rows = self.store.list_task_artifacts("AT-301")
        results = []
        self.integration.update_pr_state("AT-301", pr_merged=True, merge_commit_sha="0" * 40)
        results.append(self.cleanup(confirm_cleanup=False))
        self._merge()
        (self.worktree / "dirty.txt").write_text("x\n", encoding="utf-8")
        results.append(self.cleanup(confirm_cleanup=False))
        self.store.update_task_status("AT-301", schema.INTEGRATING, source="test")
        results.append(self.cleanup(confirm_cleanup=False))
        self.assertEqual([r.status for r in results],
                         ["merge_not_verified", "cleanup_refused", "blocked"])
        for result in results:
            self.assertFalse(result.ok)
            self.assertIsNone(result.cleanup_json_path)
        self.assertEqual(sorted((self.artifacts / "AT-301").rglob("*")), before)
        self.assertEqual(self.store.list_task_artifacts("AT-301"), rows)

    def test_a_confirmed_refusal_still_keeps_its_own_evidence(self) -> None:
        self.integration.update_pr_state("AT-301", pr_merged=True, merge_commit_sha="0" * 40)
        result = self.cleanup()
        self.assertEqual(result.status, "merge_not_verified")
        self.assertTrue(result.cleanup_json_path.is_file())


if __name__ == "__main__":
    unittest.main()
