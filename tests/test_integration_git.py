"""Tests for agent_taskflow.integration_git (spec §24, §25, §26, §36, §44)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_git as git_ops
from agent_taskflow.integration_git import IntegrationGitError
from v1_step2_fixtures import GitFixture, git  # noqa: E402


class IntegrationGitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.worktree = self.fixture.create_task_worktree("AT-601")
        self.branch = "task/AT-601"

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TargetResolutionTests(IntegrationGitTestCase):
    def test_fetch_and_resolve_latest_target(self) -> None:
        advanced = self.fixture.advance_target()
        git_ops.fetch(self.worktree, remote="origin")
        self.assertEqual(git_ops.resolve_target_sha(self.worktree, "origin", "main"), advanced)

    def test_behind_count_is_zero_when_up_to_date(self) -> None:
        git_ops.fetch(self.worktree, remote="origin")
        self.assertEqual(git_ops.behind_count(self.worktree, "HEAD", "origin/main"), 0)

    def test_behind_count_detects_an_advanced_target(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        self.fixture.advance_target("a.txt")
        self.fixture.advance_target("b.txt")
        git_ops.fetch(self.worktree, remote="origin")
        self.assertEqual(git_ops.behind_count(self.worktree, "HEAD", "origin/main"), 2)

    def test_head_sha_reads_the_worktree_head(self) -> None:
        sha = self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        self.assertEqual(git_ops.head_sha(self.worktree), sha)


class InitialRebaseTests(IntegrationGitTestCase):
    def test_initial_integration_rebases_onto_latest_target(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        advanced = self.fixture.advance_target()
        git_ops.fetch(self.worktree, remote="origin")
        result = git_ops.rebase_onto_target(self.worktree, "origin/main")
        self.assertTrue(result.ok)
        self.assertFalse(result.conflicted)
        self.assertEqual(git_ops.behind_count(self.worktree, "HEAD", "origin/main"), 0)
        self.assertIn(advanced, git_ops.rev_list(self.worktree, "HEAD"))

    def test_rebase_conflict_is_reported_and_aborted_cleanly(self) -> None:
        self.fixture.commit_in(self.worktree, "shared.txt", "task-change\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-change\n")
        git_ops.fetch(self.worktree, remote="origin")
        result = git_ops.rebase_onto_target(self.worktree, "origin/main")
        self.assertFalse(result.ok)
        self.assertTrue(result.conflicted)
        self.assertIn("shared.txt", result.conflicted_paths)
        git_ops.abort_rebase(self.worktree)
        self.assertFalse(git_ops.in_progress_operation(self.worktree))


class MergeTargetIntoBranchTests(IntegrationGitTestCase):
    def test_published_branch_merges_target_without_rewriting_history(self) -> None:
        original = self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        self.fixture.advance_target()
        git_ops.fetch(self.worktree, remote="origin")
        result = git_ops.merge_target_into_branch(self.worktree, "origin/main")
        self.assertTrue(result.ok)
        self.assertEqual(git_ops.behind_count(self.worktree, "HEAD", "origin/main"), 0)
        # The original task commit is still reachable: no history was rewritten.
        self.assertIn(original, git_ops.rev_list(self.worktree, "HEAD"))

    def test_merge_conflict_exposes_hunks_for_evidence(self) -> None:
        self.fixture.commit_in(self.worktree, "shared.txt", "task-change\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-change\n")
        git_ops.fetch(self.worktree, remote="origin")
        result = git_ops.merge_target_into_branch(self.worktree, "origin/main")
        self.assertFalse(result.ok)
        self.assertTrue(result.conflicted)
        hunks = git_ops.conflict_hunks(self.worktree)
        self.assertEqual(hunks[0]["path"], "shared.txt")
        self.assertIn("<<<<<<<", hunks[0]["hunk"])
        git_ops.abort_merge(self.worktree)
        self.assertFalse(git_ops.in_progress_operation(self.worktree))

    def test_merge_when_already_up_to_date_is_a_no_op(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        git_ops.fetch(self.worktree, remote="origin")
        before = git_ops.head_sha(self.worktree)
        result = git_ops.merge_target_into_branch(self.worktree, "origin/main")
        self.assertTrue(result.ok)
        self.assertTrue(result.already_up_to_date)
        self.assertEqual(git_ops.head_sha(self.worktree), before)


class PushSafetyTests(IntegrationGitTestCase):
    def test_normal_push_publishes_the_task_branch(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        result = git_ops.push_branch(self.worktree, remote="origin", branch=self.branch, base_branch="main")
        self.assertTrue(result.ok)
        self.assertFalse(result.force_pushed)
        self.assertIn(self.branch, git(self.fixture.origin, "branch", "--list", self.branch))

    def test_push_argv_never_contains_a_force_flag(self) -> None:
        argv = git_ops.build_push_command(remote="origin", branch=self.branch, base_branch="main")
        for flag in ("--force", "-f", "--force-with-lease", "--force-if-includes"):
            self.assertNotIn(flag, argv)

    def test_force_flags_are_rejected_at_the_guard(self) -> None:
        for flag in ("--force", "-f", "--force-with-lease"):
            with self.assertRaises(IntegrationGitError):
                git_ops.assert_no_force_push(["git", "push", flag, "origin", self.branch])

    def test_plus_refspec_force_form_is_rejected(self) -> None:
        with self.assertRaises(IntegrationGitError):
            git_ops.assert_no_force_push(["git", "push", "origin", f"+{self.branch}"])

    def test_pushing_the_target_branch_is_refused(self) -> None:
        with self.assertRaises(IntegrationGitError):
            git_ops.push_branch(self.worktree, remote="origin", branch="main", base_branch="main")

    def test_pushing_a_protected_branch_is_refused(self) -> None:
        for protected in ("main", "master", "trunk"):
            with self.assertRaises(IntegrationGitError):
                git_ops.build_push_command(remote="origin", branch=protected, base_branch="develop")

    def test_arbitrary_git_subcommands_are_not_reachable(self) -> None:
        with self.assertRaises(IntegrationGitError):
            git_ops.run_git(self.worktree, ["reset", "--hard", "origin/main"])
        with self.assertRaises(IntegrationGitError):
            git_ops.run_git(self.worktree, ["push", "--force", "origin", "main"])


class AncestryTests(IntegrationGitTestCase):
    def test_commit_contained_in_target_history(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        git_ops.push_branch(self.worktree, remote="origin", branch=self.branch, base_branch="main")
        merge_sha = self.fixture.merge_branch_into_target(self.branch)
        git_ops.fetch(self.worktree, remote="origin")
        self.assertTrue(git_ops.commit_in_history(self.worktree, merge_sha, "origin/main"))

    def test_commit_not_contained_in_target_history(self) -> None:
        task_sha = self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        git_ops.fetch(self.worktree, remote="origin")
        self.assertFalse(git_ops.commit_in_history(self.worktree, task_sha, "origin/main"))

    def test_unknown_commit_is_not_contained(self) -> None:
        git_ops.fetch(self.worktree, remote="origin")
        self.assertFalse(git_ops.commit_in_history(self.worktree, "0" * 40, "origin/main"))


class DiffContextTests(IntegrationGitTestCase):
    def test_diff_context_lists_changed_files_against_the_target(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        git_ops.fetch(self.worktree, remote="origin")
        context = git_ops.diff_context(self.worktree, "origin/main")
        self.assertIn("feature.txt", context)
        self.assertIn("feature.txt", git_ops.changed_files(self.worktree, "origin/main"))


if __name__ == "__main__":
    unittest.main()
