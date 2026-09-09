"""Tests for agent_taskflow.github_pr_adapter (spec §31, §32, §34, §44)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow.github_pr_adapter import (
    GitHubPrAdapter,
    GitHubPrError,
    assert_not_a_merge_command,
)
from v1_step2_fixtures import FakeGhRunner  # noqa: E402


class CreateAndUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeGhRunner()
        self.adapter = GitHubPrAdapter("owner/repo", runner=self.runner)

    def test_create_pr_returns_a_snapshot(self) -> None:
        snapshot = self.adapter.create_pr(
            base="main",
            head="task/AT-101",
            title="AT-101",
            body="body",
            cwd=Path("/tmp"),
        )
        self.assertEqual(snapshot.number, 42)
        self.assertEqual(snapshot.url, "https://github.com/owner/repo/pull/42")
        self.assertEqual(snapshot.state, "open")
        self.assertIs(snapshot.merged, False)

    def test_create_pr_is_draft_by_default(self) -> None:
        self.adapter.create_pr(base="main", head="task/AT-101", title="t", body="b", cwd=Path("/tmp"))
        create_call = next(call for call in self.runner.calls if call[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create_call)

    def test_update_pr_targets_the_same_pr_number(self) -> None:
        created = self.adapter.create_pr(
            base="main", head="task/AT-101", title="t", body="b", cwd=Path("/tmp")
        )
        updated = self.adapter.update_pr(
            pr_number=created.number, body="updated body", cwd=Path("/tmp")
        )
        self.assertEqual(updated.number, created.number)
        edit_call = next(call for call in self.runner.calls if call[:3] == ["gh", "pr", "edit"])
        self.assertEqual(edit_call[3], str(created.number))

    def test_create_failure_raises(self) -> None:
        self.runner.create_returncode = 1
        self.runner.create_stderr = "boom"
        with self.assertRaises(GitHubPrError):
            self.adapter.create_pr(base="main", head="h", title="t", body="b", cwd=Path("/tmp"))


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeGhRunner()
        self.adapter = GitHubPrAdapter("owner/repo", runner=self.runner)
        self.runner.set_pr(
            42,
            state="OPEN",
            merged=False,
            headRefOid="head1",
            baseRefName="main",
            headRefName="task/AT-101",
            reviewDecision="REVIEW_REQUIRED",
            statusCheckRollup=[{"state": "SUCCESS"}],
        )

    def test_poll_maps_review_decision_to_the_spec_enum(self) -> None:
        cases = {
            "": "none",
            "REVIEW_REQUIRED": "none",
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes_requested",
        }
        for raw, expected in cases.items():
            self.runner.set_pr(42, reviewDecision=raw)
            self.assertEqual(self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp")).review_decision, expected)

    def test_poll_maps_ci_status_to_the_spec_enum(self) -> None:
        cases = [
            ([], "none"),
            ([{"state": "SUCCESS"}], "success"),
            ([{"state": "SUCCESS"}, {"state": "PENDING"}], "pending"),
            ([{"state": "SUCCESS"}, {"state": "FAILURE"}], "failure"),
            ([{"conclusion": "FAILURE", "status": "COMPLETED"}], "failure"),
        ]
        for rollup, expected in cases:
            self.runner.set_pr(42, statusCheckRollup=rollup)
            self.assertEqual(self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp")).ci_status, expected)

    def test_poll_reads_merge_identity(self) -> None:
        self.runner.set_pr(
            42,
            state="MERGED",
            merged=True,
            mergedAt="2026-09-10T04:00:00Z",
            mergeCommit={"oid": "mergesha"},
        )
        snapshot = self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp"))
        self.assertIs(snapshot.merged, True)
        self.assertEqual(snapshot.merge_commit_sha, "mergesha")
        self.assertEqual(snapshot.merged_at, "2026-09-10T04:00:00Z")
        self.assertEqual(snapshot.state, "closed")

    def test_poll_is_read_only_and_repeatable(self) -> None:
        first = self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp"))
        second = self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp"))
        self.assertEqual(first.number, second.number)
        self.assertEqual(first.state, second.state)
        for call in self.runner.calls:
            self.assertEqual(call[:3], ["gh", "pr", "view"])

    def test_poll_collects_review_comments(self) -> None:
        self.runner.set_pr(
            42,
            reviewDecision="CHANGES_REQUESTED",
            reviews=[
                {
                    "author": {"login": "octocat"},
                    "state": "CHANGES_REQUESTED",
                    "body": "please fix",
                    "submittedAt": "2026-09-10T03:00:00Z",
                }
            ],
        )
        snapshot = self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp"))
        self.assertEqual(snapshot.reviews[0]["author"], "octocat")
        self.assertEqual(snapshot.reviews[0]["body"], "please fix")


class MergeIsForbiddenTests(unittest.TestCase):
    def test_adapter_has_no_merge_method(self) -> None:
        public = [name for name in dir(GitHubPrAdapter) if not name.startswith("_")]
        self.assertEqual([name for name in public if "merge" in name.lower()], [])

    def test_merge_subcommand_is_rejected_by_the_guard(self) -> None:
        for argv in (
            ["gh", "pr", "merge", "42"],
            ["gh", "pr", "merge", "42", "--squash"],
            ["gh", "api", "-X", "PUT", "repos/owner/repo/pulls/42/merge"],
            ["git", "merge", "task/AT-101"],
            ["git", "push", "origin", "HEAD:main"],
        ):
            with self.assertRaises(GitHubPrError):
                assert_not_a_merge_command(argv)

    def test_ordinary_pr_commands_pass_the_guard(self) -> None:
        for argv in (
            ["gh", "pr", "create", "--draft"],
            ["gh", "pr", "edit", "42", "--body", "x"],
            ["gh", "pr", "view", "42", "--json", "state"],
        ):
            assert_not_a_merge_command(argv)

    def test_adapter_refuses_to_run_a_merge_argv(self) -> None:
        adapter = GitHubPrAdapter("owner/repo", runner=FakeGhRunner())
        with self.assertRaises(GitHubPrError):
            adapter.run(["gh", "pr", "merge", "42"], cwd=Path("/tmp"))


if __name__ == "__main__":
    unittest.main()
