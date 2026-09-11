"""Tests for agent_taskflow.integration_git (spec §24, §25, §26, §36, §44)."""

from __future__ import annotations

import sys
import tempfile
import os
import unittest
from unittest import mock
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
    """Review Ruling 3 — the push guard is an allowlist."""

    def test_normal_push_publishes_the_task_branch(self) -> None:
        self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        result = git_ops.push_branch(self.worktree, remote="origin", branch=self.branch, base_branch="main")
        self.assertTrue(result.ok)
        self.assertFalse(result.force_pushed)
        self.assertIn(self.branch, git(self.fixture.origin, "branch", "--list", self.branch))

    def test_built_push_argv_is_exactly_git_push_origin_task_branch(self) -> None:
        self.assertEqual(
            git_ops.build_push_command(remote="origin", branch=self.branch, base_branch="main"),
            ("git", "push", "origin", self.branch),
        )
        self.assertEqual(
            git_ops.build_push_command(
                remote="origin", branch=self.branch, base_branch="main", set_upstream=True
            ),
            ("git", "push", "-u", "origin", self.branch),
        )

    def test_the_allowlist_accepts_exactly_the_two_permitted_forms(self) -> None:
        for argv in (
            ["git", "push", "origin", self.branch],
            ["git", "push", "-u", "origin", self.branch],
        ):
            with self.subTest(argv=argv):
                git_ops.assert_push_allowed(argv, task_branch=self.branch, base_branch="main")

    def test_the_push_allowlist_refuses_each_listed_form(self) -> None:
        b = self.branch
        refused = (
            ["git", "push", "--force", "origin", b],
            ["git", "push", "-f", "origin", b],
            ["git", "push", "--force-with-lease", "origin", b],
            ["git", "push", f"--force-with-lease={b}", "origin", b],
            ["git", "push", "--force-if-includes", "origin", b],
            ["git", "push", "--mirror", "origin"],
            ["git", "push", "--all", "origin"],
            ["git", "push", "--tags", "origin"],
            ["git", "push", "origin", "--delete", b],
            ["git", "push", "--delete", "origin", b],
            ["git", "push", "origin", f"{b}:{b}"],
            ["git", "push", "origin", f"HEAD:{b}"],
            ["git", "push", "origin", "HEAD:main"],
            ["git", "push", "-vf", "origin", b],
            ["git", "push", "-uf", "origin", b],
            ["git", "push", "origin", f"+{b}"],
            ["git", "push", "--set-upstream", "origin", b],
            ["git", "push", "upstream", b],
            ["git", "push", "origin", "task/another-ticket"],
            ["git", "push", "origin", b, "task/another-ticket"],
            ["git", "push", "origin"],
            ["git", "push"],
            ["git", "push", "origin", "main"],
            ["git", "push", "-u", "origin", "main"],
        )
        for argv in refused:
            with self.subTest(argv=argv):
                with self.assertRaises(IntegrationGitError):
                    git_ops.assert_push_allowed(argv, task_branch=b, base_branch="main")

    def test_the_task_branch_itself_may_not_be_main_protected_or_the_base_branch(self) -> None:
        for branch, base in (
            ("main", "main"),
            ("master", "develop"),
            ("trunk", "develop"),
            ("develop", "develop"),
        ):
            with self.subTest(branch=branch, base=base):
                with self.assertRaises(IntegrationGitError):
                    git_ops.assert_push_allowed(
                        ["git", "push", "origin", branch], task_branch=branch, base_branch=base
                    )

    def test_pushing_the_target_branch_is_refused(self) -> None:
        with self.assertRaises(IntegrationGitError):
            git_ops.push_branch(self.worktree, remote="origin", branch="main", base_branch="main")

    def test_pushing_a_protected_branch_is_refused(self) -> None:
        for protected in ("main", "master", "trunk"):
            with self.assertRaises(IntegrationGitError):
                git_ops.build_push_command(remote="origin", branch=protected, base_branch="develop")

    def test_run_git_refuses_a_push_that_declares_no_task_branch(self) -> None:
        with self.assertRaises(IntegrationGitError):
            git_ops.run_git(self.worktree, ["push", "origin", self.branch])

    def test_run_git_refuses_reset_and_a_forced_push_to_main(self) -> None:
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



class ResolutionVerificationTests(IntegrationGitTestCase):
    """Review blocker B2 — the five deterministic post-resolution checks.

    Each test builds a state in which exactly one check fails and asserts it
    is the only failure reported, so each check is shown to work on its own.
    """

    def setUp(self) -> None:
        super().setUp()
        self.target_sha = self.fixture.target_sha()
        self.head_before = git_ops.head_sha(self.worktree)
        self.fixture.commit_in(self.worktree, "feature.txt", "resolved\n", "resolution")

    def verify(self, **overrides):
        kwargs = dict(
            head_before=self.head_before,
            target_sha=self.target_sha,
            conflicted_files=("feature.txt",),
        )
        kwargs.update(overrides)
        return git_ops.verify_conflict_resolution(self.worktree, **kwargs)

    def test_a_clean_committed_resolution_passes_all_five_checks(self) -> None:
        verification = self.verify()
        self.assertTrue(verification.passed)
        self.assertEqual(verification.failed, ())
        self.assertEqual([c.name for c in verification.checks], list(git_ops.RESOLUTION_CHECKS))

    def test_check_a_merge_head_fails_only_check_a(self) -> None:
        path = git_ops.git_dir(self.worktree) / "MERGE_HEAD"
        path.write_text(self.target_sha + "\n", encoding="utf-8")
        try:
            self.assertEqual(self.verify().failed, (git_ops.CHECK_NO_OPERATION_IN_PROGRESS,))
        finally:
            path.unlink()

    def test_check_a_a_leftover_rebase_head_alone_does_not_fail_check_a(self) -> None:
        """Amended check (a): git 2.43 leaves REBASE_HEAD behind after a
        successful ``rebase --continue``; on its own it is not in progress."""
        path = git_ops.git_dir(self.worktree) / "REBASE_HEAD"
        path.write_text(self.target_sha + "\n", encoding="utf-8")
        try:
            self.assertIsNone(git_ops.in_progress_operation(self.worktree))
            self.assertTrue(self.verify().passed)
        finally:
            path.unlink()

    def test_check_a_a_stopped_rebase_fails_check_a(self) -> None:
        """A real rebase stopped on a conflict has rebase-merge/, so it is caught."""
        self.fixture.commit_in(self.worktree, "shared.txt", "task-side\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-side\n")
        git_ops.fetch(self.worktree, remote="origin")
        self.assertTrue(git_ops.rebase_onto_target(self.worktree, "origin/main").conflicted)
        self.assertTrue((git_ops.git_dir(self.worktree) / "rebase-merge").is_dir())
        self.assertEqual(git_ops.in_progress_operation(self.worktree), "rebase")
        self.assertIn(git_ops.CHECK_NO_OPERATION_IN_PROGRESS, self.verify().failed)

    def test_check_a_rebase_directories_count_as_an_operation_in_progress(self) -> None:
        directory = git_ops.git_dir(self.worktree)
        for marker in ("rebase-merge", "rebase-apply"):
            with self.subTest(marker=marker):
                (directory / marker).mkdir()
                try:
                    self.assertEqual(git_ops.in_progress_operation(self.worktree), "rebase")
                finally:
                    (directory / marker).rmdir()

    def test_check_b_an_untracked_file_fails_only_check_b(self) -> None:
        (self.worktree / "resolver-notes.orig").write_text("left behind\n", encoding="utf-8")
        self.assertEqual(self.verify().failed, (git_ops.CHECK_WORKTREE_CLEAN,))

    def test_check_c_committed_conflict_markers_fail_only_check_c(self) -> None:
        self.fixture.commit_in(
            self.worktree,
            "feature.txt",
            "<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n",
            "committed with markers",
        )
        self.assertEqual(self.verify().failed, (git_ops.CHECK_NO_CONFLICT_MARKERS,))

    def test_check_c_a_lone_separator_counts_only_in_a_conflicted_file(self) -> None:
        self.fixture.commit_in(self.worktree, "doc.txt", "Title\n=======\n", "setext heading")
        self.assertEqual(
            self.verify(conflicted_files=("doc.txt",)).failed,
            (git_ops.CHECK_NO_CONFLICT_MARKERS,),
        )
        self.assertTrue(self.verify(conflicted_files=("feature.txt",)).passed)

    def test_check_d_an_unchanged_head_fails_only_check_d(self) -> None:
        verification = self.verify(head_before=git_ops.head_sha(self.worktree))
        self.assertEqual(verification.failed, (git_ops.CHECK_HEAD_IS_NEW_COMMIT,))

    def test_check_e_a_target_missing_from_head_fails_only_check_e(self) -> None:
        advanced = self.fixture.advance_target("other.txt")
        git_ops.fetch(self.worktree, remote="origin")
        self.assertEqual(
            self.verify(target_sha=advanced).failed, (git_ops.CHECK_TARGET_IS_ANCESTOR,)
        )



class NonInteractiveGitTests(IntegrationGitTestCase):
    def test_a_conflicted_rebase_is_continued_without_an_editor(self) -> None:
        """Regression found by review blocker B2's check (a).

        With no TTY, `git rebase --continue` failed on the editor and silently
        left the rebase in progress. The control plane now forces a
        non-interactive editor, so this passes even when the environment
        names an editor that always fails.
        """
        self.fixture.commit_in(self.worktree, "shared.txt", "task-side\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-side\n")
        git_ops.fetch(self.worktree, remote="origin")
        self.assertTrue(git_ops.rebase_onto_target(self.worktree, "origin/main").conflicted)
        (self.worktree / "shared.txt").write_text("resolved\n", encoding="utf-8")
        git_ops.stage_all(self.worktree)
        with mock.patch.dict(os.environ, {"GIT_EDITOR": "false", "EDITOR": "false"}):
            result = git_ops.commit_conflict_resolution(self.worktree, "resolve")
        self.assertTrue(result.ok, result.combined)
        self.assertIsNone(git_ops.in_progress_operation(self.worktree))



class HeadOnTaskBranchTests(IntegrationGitTestCase):
    """Review round 4, Ruling 30 — assert_head_on_task_branch."""

    def test_head_on_the_task_branch_returns_its_tip(self) -> None:
        tip = git_ops.assert_head_on_task_branch(self.worktree, self.branch)
        self.assertEqual(tip, git_ops.head_sha(self.worktree))

    def test_a_recorded_refs_heads_name_is_accepted(self) -> None:
        tip = git_ops.assert_head_on_task_branch(self.worktree, f"refs/heads/{self.branch}")
        self.assertEqual(tip, git_ops.head_sha(self.worktree))

    def test_a_detached_head_is_refused(self) -> None:
        git(self.worktree, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(IntegrationGitError, "detached"):
            git_ops.assert_head_on_task_branch(self.worktree, self.branch)

    def test_head_on_another_branch_is_refused(self) -> None:
        git(self.worktree, "checkout", "-q", "-b", "task/other")
        with self.assertRaisesRegex(IntegrationGitError, "refs/heads/task/other"):
            git_ops.assert_head_on_task_branch(self.worktree, self.branch)

    def test_a_branch_that_moved_off_the_validated_commit_is_refused(self) -> None:
        validated = git_ops.head_sha(self.worktree)
        self.fixture.commit_in(self.worktree, "later.txt", "x\n", "moved after validation")
        with self.assertRaisesRegex(IntegrationGitError, "moved"):
            git_ops.assert_head_on_task_branch(
                self.worktree, self.branch, expected_sha=validated
            )

    def test_the_check_is_read_only(self) -> None:
        log = git_ops.GitCommandLog()
        git_ops.assert_head_on_task_branch(self.worktree, self.branch, log=log)
        self.assertEqual({argv[1] for argv in log.commands}, {"rev-parse"})


class OutputDecodingTests(IntegrationGitTestCase):
    """Ruling 31b — git output that is not UTF-8 is decoded with replacement."""

    def test_non_utf8_git_output_does_not_raise(self) -> None:
        # `git show <blob>` prints the stored bytes verbatim; \xe9 alone is not
        # valid UTF-8, so strict decoding would raise UnicodeDecodeError.
        (self.worktree / "latin1.txt").write_bytes(b"caf\xe9 latin-1 content\n")
        git(self.worktree, "add", "latin1.txt")
        git(self.worktree, "commit", "-q", "-m", "add a latin-1 file")
        result = git_ops.run_git(self.worktree, ["show", "HEAD:latin1.txt"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "caf\ufffd latin-1 content\n")


class BranchNormalizationTests(IntegrationGitTestCase):
    """Review Ruling 18 — branch names are compared only after normalization."""

    def test_normalize_strips_whitespace_and_exactly_one_heads_prefix(self) -> None:
        for raw, expected in (
            ("main", "main"),
            ("refs/heads/main", "main"),
            ("heads/main", "main"),
            ("  refs/heads/main\t", "main"),
            ("\nheads/develop\n", "develop"),
            ("refs/heads/task/AT-601", "task/AT-601"),
            # git reads these as other branches, literally named
            # `refs/heads/main` and `heads/main`, so only one prefix goes.
            ("refs/heads/refs/heads/main", "refs/heads/main"),
            ("heads/heads/main", "heads/main"),
            # Case is kept: git refs are case-sensitive.
            ("refs/heads/Main", "Main"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(git_ops.normalize_branch_ref(raw), expected)

    def test_a_task_branch_naming_main_or_the_base_branch_in_any_listed_form_is_refused(self) -> None:
        for task_branch in (
            "refs/heads/main",
            "heads/main",
            "refs/heads/develop",
            "heads/develop",
            "develop",
            "  main  ",
            "  refs/heads/main\t",
            "\nheads/develop\n",
            "refs/heads/master",
            "heads/trunk",
            "HEAD",
        ):
            with self.subTest(task_branch=task_branch):
                with self.assertRaises(IntegrationGitError):
                    git_ops.assert_task_branch_pushable(task_branch, base_branch="develop")
                with self.assertRaises(IntegrationGitError):
                    git_ops.assert_push_allowed(
                        ["git", "push", "origin", task_branch.strip()],
                        task_branch=task_branch,
                        base_branch="develop",
                    )

    def test_a_push_target_naming_main_is_refused_even_for_a_legitimate_task_branch(self) -> None:
        for target in ("refs/heads/main", "heads/main", "refs/heads/develop", "heads/develop"):
            with self.subTest(target=target):
                with self.assertRaises(IntegrationGitError):
                    git_ops.assert_push_allowed(
                        ["git", "push", "origin", target],
                        task_branch=self.branch,
                        base_branch="develop",
                    )

    def test_the_target_must_normalize_to_the_tickets_own_task_branch(self) -> None:
        for target in (self.branch, f"refs/heads/{self.branch}", f"heads/{self.branch}"):
            with self.subTest(allowed=target):
                git_ops.assert_push_allowed(
                    ["git", "push", "origin", target], task_branch=self.branch, base_branch="main"
                )
        for target in ("task/another-ticket", "refs/heads/task/another-ticket", f" {self.branch}"):
            with self.subTest(refused=target):
                with self.assertRaises(IntegrationGitError):
                    git_ops.assert_push_allowed(
                        ["git", "push", "origin", target], task_branch=self.branch, base_branch="main"
                    )


if __name__ == "__main__":
    unittest.main()
