"""Tests for agent_taskflow.merge_verification (spec §35, §36, §36.1)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_git as git_ops
from agent_taskflow.merge_verification import MergeVerificationRequest, verify_merge
from v1_step2_fixtures import GitFixture  # noqa: E402


class MergeVerificationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.worktree = self.fixture.create_task_worktree("AT-901")
        self.branch = "task/AT-901"
        self.task_sha = self.fixture.commit_in(self.worktree, "feature.txt", "feature\n", "feature")
        git_ops.push_branch(self.worktree, remote="origin", branch=self.branch, base_branch="main")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, **overrides):
        kwargs = dict(
            task_key="AT-901",
            worktree_path=self.worktree,
            remote="origin",
            target_branch="main",
            pr_merged=True,
            merge_commit_sha=None,
        )
        kwargs.update(overrides)
        return MergeVerificationRequest(**kwargs)


class MergeMethodTests(MergeVerificationTestCase):
    def test_merge_commit_is_verified(self) -> None:
        merge_sha = self.fixture.merge_branch_into_target(self.branch, method="merge")
        result = verify_merge(self._request(merge_commit_sha=merge_sha))
        self.assertTrue(result.verified)
        self.assertTrue(result.contained_in_target)

    def test_squash_merge_is_verified(self) -> None:
        merge_sha = self.fixture.merge_branch_into_target(self.branch, method="squash")
        result = verify_merge(self._request(merge_commit_sha=merge_sha))
        self.assertTrue(result.verified)

    def test_rebase_merge_is_verified(self) -> None:
        merge_sha = self.fixture.merge_branch_into_target(self.branch, method="rebase")
        result = verify_merge(self._request(merge_commit_sha=merge_sha))
        self.assertTrue(result.verified)

    def test_squash_and_rebase_merges_do_not_keep_the_original_task_sha(self) -> None:
        """§36 — the original task SHA is not a valid merge identity."""
        for method in ("squash", "rebase"):
            with self.subTest(method=method):
                fixture = GitFixture(Path(tempfile.mkdtemp(dir=self.root)))
                worktree = fixture.create_task_worktree("AT-902")
                task_sha = fixture.commit_in(worktree, "f.txt", "f\n", "f")
                git_ops.push_branch(
                    worktree, remote="origin", branch="task/AT-902", base_branch="main"
                )
                fixture.merge_branch_into_target("task/AT-902", method=method)
                git_ops.fetch(worktree, remote="origin")
                self.assertFalse(git_ops.commit_in_history(worktree, task_sha, "origin/main"))


class VerificationGateTests(MergeVerificationTestCase):
    def test_not_merged_is_not_verified(self) -> None:
        result = verify_merge(self._request(pr_merged=False, merge_commit_sha="abc"))
        self.assertFalse(result.verified)
        self.assertIn("pr_merged", " ".join(result.reasons))

    def test_missing_merge_commit_sha_is_not_verified(self) -> None:
        result = verify_merge(self._request(pr_merged=True, merge_commit_sha=None))
        self.assertFalse(result.verified)
        self.assertIn("merge_commit_sha", " ".join(result.reasons))

    def test_merge_commit_absent_from_target_history_is_not_verified(self) -> None:
        result = verify_merge(self._request(merge_commit_sha=self.task_sha))
        self.assertFalse(result.verified)
        self.assertFalse(result.contained_in_target)

    def test_unknown_sha_is_not_verified(self) -> None:
        result = verify_merge(self._request(merge_commit_sha="0" * 40))
        self.assertFalse(result.verified)

    def test_verification_fetches_the_latest_target(self) -> None:
        merge_sha = self.fixture.merge_branch_into_target(self.branch, method="merge")
        # The worktree has not fetched yet; verify_merge must fetch for itself.
        result = verify_merge(self._request(merge_commit_sha=merge_sha))
        self.assertTrue(result.verified)
        self.assertEqual(result.target_sha, self.fixture.target_sha())

    def test_result_records_that_the_task_sha_was_not_used(self) -> None:
        merge_sha = self.fixture.merge_branch_into_target(self.branch, method="merge")
        result = verify_merge(self._request(merge_commit_sha=merge_sha))
        self.assertEqual(result.merge_commit_sha, merge_sha)
        self.assertIs(result.original_task_sha_used, False)


if __name__ == "__main__":
    unittest.main()
