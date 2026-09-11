"""Tests for agent_taskflow.github_pr_adapter (spec §31, §32, §34, §44)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import github_pr_adapter
from agent_taskflow.github_pr_adapter import (
    PR_VIEW_FIELDS,
    GitHubPrAdapter,
    GitHubPrError,
    assert_gh_api_allowed,
    assert_not_a_merge_command,
)
from v1_step2_fixtures import (  # noqa: E402
    GH_PR_EDIT_PROJECTS_CLASSIC_ERROR,
    GH_PR_VIEW_JSON_FIELDS,
    FakeCompletedProcess,
    FakeGhRunner,
)


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
        patch_call = next(call for call in self.runner.calls if call[:2] == ["gh", "api"])
        self.assertEqual(patch_call[4], f"repos/owner/repo/pulls/{created.number}")

    def test_create_failure_raises(self) -> None:
        self.runner.create_returncode = 1
        self.runner.create_stderr = "boom"
        with self.assertRaises(GitHubPrError):
            self.adapter.create_pr(base="main", head="h", title="t", body="b", cwd=Path("/tmp"))


class RealGhFieldContractTests(unittest.TestCase):
    """Review round 4, Ruling 29c — the adapter may ask only for fields real
    `gh pr view --json` has. The fake knows exactly gh 2.45.0's list, so any
    field real gh would reject fails here too."""

    def setUp(self) -> None:
        self.runner = FakeGhRunner()
        self.adapter = GitHubPrAdapter("owner/repo", runner=self.runner)
        self.runner.set_pr(42, state="OPEN")

    def test_every_requested_field_is_one_real_gh_accepts(self) -> None:
        unknown = [name for name in PR_VIEW_FIELDS if name not in GH_PR_VIEW_JSON_FIELDS]
        self.assertEqual(unknown, [])
        self.assertNotIn("merged", PR_VIEW_FIELDS)

    def test_the_fake_rejects_an_unknown_field_as_real_gh_does(self) -> None:
        completed = self.runner(
            ["gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "number,merged"]
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn('Unknown JSON field: "merged"', completed.stderr)
        self.assertIn("Available fields:", completed.stderr)

    def test_a_field_list_real_gh_rejects_fails_the_poll(self) -> None:
        with mock.patch.object(
            github_pr_adapter, "PR_VIEW_FIELDS", PR_VIEW_FIELDS + ("merged",)
        ):
            with self.assertRaises(GitHubPrError) as caught:
                self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp"))
        self.assertIn("Unknown JSON field", str(caught.exception))

    def test_the_fake_cannot_be_scripted_with_a_field_real_gh_lacks(self) -> None:
        with self.assertRaises(AssertionError):
            self.runner.set_pr(42, merged=True)


class RealGhPayloadTests(unittest.TestCase):
    """The values real gh 2.45.0 returned for the adapter's field list on this
    repository — #196 open, #200 merged — with title, body and the check
    rollup abbreviated. Pins that the real shapes parse as intended."""

    OPEN = {
        "number": 196, "url": "https://github.com/anderson930420/agent-taskflow/pull/196",
        "state": "OPEN", "isDraft": True, "mergedAt": None, "mergeCommit": None,
        "headRefName": "task/v1-step2", "baseRefName": "main",
        "headRefOid": "25143529eea66560138feef2e4326b436b28c6ab", "reviewDecision": "",
        "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        "reviews": [], "title": "V1 Step 2", "body": "",
    }
    MERGED = {
        "number": 200, "url": "https://github.com/anderson930420/agent-taskflow/pull/200",
        "state": "MERGED", "isDraft": False, "mergedAt": "2026-09-11T09:54:01Z",
        "mergeCommit": {"oid": "a5fa8e2daee56d026ad11068122e99eb5fc78281"},
        "headRefName": "task/v1-step4", "baseRefName": "main",
        "headRefOid": "4f3bdb87350d12c536ec92f900de6ab856e66469", "reviewDecision": "",
        "statusCheckRollup": [], "reviews": [], "title": "V1 Step 4", "body": "",
    }

    def poll(self, payload: dict) -> "github_pr_adapter.PrSnapshot":
        self.assertEqual(set(payload), set(PR_VIEW_FIELDS))

        def runner(args, **kwargs):
            return FakeCompletedProcess(returncode=0, stdout=json.dumps(payload))

        adapter = GitHubPrAdapter("anderson930420/agent-taskflow", runner=runner)
        return adapter.poll_pr(pr_number=payload["number"], cwd=Path("/tmp"))

    def test_an_open_real_pr_reads_as_open_and_unmerged(self) -> None:
        snapshot = self.poll(self.OPEN)
        self.assertEqual(snapshot.state, "open")
        self.assertIs(snapshot.merged, False)
        self.assertIsNone(snapshot.merge_commit_sha)

    def test_a_merged_real_pr_reads_as_merged_with_its_merge_commit(self) -> None:
        snapshot = self.poll(self.MERGED)
        self.assertEqual(snapshot.state, "closed")
        self.assertIs(snapshot.merged, True)
        self.assertEqual(snapshot.merge_commit_sha, "a5fa8e2daee56d026ad11068122e99eb5fc78281")
        self.assertEqual(snapshot.merged_at, "2026-09-11T09:54:01Z")


class MergeDerivationTests(unittest.TestCase):
    """Ruling 29a — merged is derived from state, mergedAt and mergeCommit."""

    def setUp(self) -> None:
        self.runner = FakeGhRunner()
        self.adapter = GitHubPrAdapter("owner/repo", runner=self.runner)

    def snapshot(self, **fields):
        self.runner.set_pr(42, **fields)
        return self.adapter.poll_pr(pr_number=42, cwd=Path("/tmp"))

    def test_state_merged_alone_means_merged(self) -> None:
        snapshot = self.snapshot(state="MERGED")
        self.assertIs(snapshot.merged, True)
        self.assertEqual(snapshot.state, "closed")

    def test_a_merged_at_time_means_merged(self) -> None:
        self.assertIs(self.snapshot(state="CLOSED", mergedAt="2026-09-10T00:00:00Z").merged, True)

    def test_a_merge_commit_means_merged(self) -> None:
        self.assertIs(self.snapshot(state="CLOSED", mergeCommit={"oid": "abc"}).merged, True)

    def test_closed_without_merge_evidence_is_closed_unmerged(self) -> None:
        snapshot = self.snapshot(state="CLOSED")
        self.assertIs(snapshot.merged, False)
        self.assertEqual(snapshot.state, "closed")

    def test_open_is_not_merged(self) -> None:
        snapshot = self.snapshot(state="OPEN")
        self.assertIs(snapshot.merged, False)
        self.assertEqual(snapshot.state, "open")


class CreateIdentityTests(unittest.TestCase):
    """Ruling 29d — create_pr returns the identity gh printed; it never polls."""

    def test_create_pr_does_not_poll(self) -> None:
        runner = FakeGhRunner()
        adapter = GitHubPrAdapter("owner/repo", runner=runner)
        snapshot = adapter.create_pr(
            base="main", head="task/AT-101", title="t", body="b", cwd=Path("/tmp")
        )
        self.assertEqual([call[:3] for call in runner.calls], [["gh", "pr", "create"]])
        self.assertEqual(snapshot.number, 42)
        self.assertEqual(snapshot.url, "https://github.com/owner/repo/pull/42")
        self.assertEqual(snapshot.head_ref, "task/AT-101")
        self.assertEqual(snapshot.base_ref, "main")
        self.assertIs(snapshot.is_draft, True)

    def test_create_pr_succeeds_even_when_a_poll_would_fail(self) -> None:
        def runner(args, **kwargs):
            if args[:3] == ["gh", "pr", "create"]:
                return FakeCompletedProcess(
                    returncode=0, stdout="https://github.com/owner/repo/pull/7\n"
                )
            return FakeCompletedProcess(returncode=1, stderr='Unknown JSON field: "merged"')

        snapshot = GitHubPrAdapter("owner/repo", runner=runner).create_pr(
            base="main", head="task/AT-101", title="t", body="b", cwd=Path("/tmp")
        )
        self.assertEqual(snapshot.number, 7)
        self.assertEqual(snapshot.url, "https://github.com/owner/repo/pull/7")


class DecodingTests(unittest.TestCase):
    """Ruling 31b — gh output is decoded with replacement, never raising."""

    def test_gh_is_run_with_replacement_decoding(self) -> None:
        seen: list[dict] = []

        def runner(args, **kwargs):
            seen.append(kwargs)
            return FakeCompletedProcess(returncode=0, stdout="{}")

        GitHubPrAdapter("owner/repo", runner=runner).run(["gh", "pr", "view", "1"], cwd=Path("/tmp"))
        self.assertEqual(seen[0]["errors"], "replace")
        self.assertEqual(seen[0]["encoding"], "utf-8")


class PollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeGhRunner()
        self.adapter = GitHubPrAdapter("owner/repo", runner=self.runner)
        self.runner.set_pr(
            42,
            state="OPEN",
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


class RestUpdateTests(unittest.TestCase):
    """Review Ruling 35a/c — update_pr is a REST PATCH, never `gh pr edit`."""

    def setUp(self) -> None:
        self.runner = FakeGhRunner()
        self.adapter = GitHubPrAdapter("owner/repo", runner=self.runner)
        self.runner.set_pr(42, state="OPEN", title="old title", body="old body")

    def api_calls(self) -> list[list[str]]:
        return [call for call in self.runner.calls if call[:2] == ["gh", "api"]]

    def test_the_fake_fails_gh_pr_edit_exactly_like_real_gh(self) -> None:
        completed = self.runner(
            ["gh", "pr", "edit", "42", "--repo", "owner/repo", "--body", "x"]
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, GH_PR_EDIT_PROJECTS_CLASSIC_ERROR)
        self.assertIn("Projects (classic) is being deprecated", completed.stderr)
        self.assertEqual(self.runner.pulls[42]["body"], "old body")

    def test_update_pr_sends_one_patch_with_the_body(self) -> None:
        snapshot = self.adapter.update_pr(pr_number=42, body="new body", cwd=Path("/tmp"))
        self.assertEqual(
            self.api_calls(),
            [["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=new body"]],
        )
        self.assertEqual(snapshot.number, 42)
        self.assertEqual(snapshot.body, "new body")
        self.assertEqual(self.runner.pulls[42]["title"], "old title")

    def test_update_pr_sends_title_and_body_as_raw_fields(self) -> None:
        self.adapter.update_pr(pr_number=42, title="t2", body="b2", cwd=Path("/tmp"))
        self.assertEqual(
            self.api_calls()[0][5:], ["-f", "title=t2", "-f", "body=b2"]
        )
        self.assertEqual(self.runner.pulls[42]["title"], "t2")
        self.assertEqual(self.runner.pulls[42]["body"], "b2")

    def test_update_pr_never_calls_gh_pr_edit(self) -> None:
        self.adapter.update_pr(pr_number=42, body="x", cwd=Path("/tmp"))
        self.assertEqual([c for c in self.runner.calls if c[:3] == ["gh", "pr", "edit"]], [])

    def test_a_body_is_sent_literally(self) -> None:
        # -f, not -F: `@file` is never read and `a=b` keeps its `=`.
        for body in ("@/etc/passwd", "a=b=c", "-starts-with-dash", "true", "merge me"):
            with self.subTest(body=body):
                self.adapter.update_pr(pr_number=42, body=body, cwd=Path("/tmp"))
                self.assertEqual(self.api_calls()[-1][-1], f"body={body}")
                self.assertEqual(self.runner.pulls[42]["body"], body)

    def test_update_pr_returns_a_poll_of_the_same_pr(self) -> None:
        snapshot = self.adapter.update_pr(pr_number=42, body="x", cwd=Path("/tmp"))
        self.assertEqual(self.runner.calls[-1][:4], ["gh", "pr", "view", "42"])
        self.assertEqual(snapshot.state, "open")

    def test_update_pr_with_no_field_only_polls(self) -> None:
        self.adapter.update_pr(pr_number=42, cwd=Path("/tmp"))
        self.assertEqual(self.api_calls(), [])

    def test_a_failed_patch_raises(self) -> None:
        with self.assertRaises(GitHubPrError) as caught:
            self.adapter.update_pr(pr_number=99, body="x", cwd=Path("/tmp"))
        self.assertIn("HTTP 404", str(caught.exception))


class GhApiAllowlistTests(unittest.TestCase):
    """Review Ruling 35b — every `gh api` argv passes an allowlist that admits
    only the exact REST update the adapter issues."""

    ADMITTED = (
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "title=t", "-f", "body=b"],
        ["/usr/bin/gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/7", "-f", "title=t"],
        ["env", "gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=x"],
    )

    REFUSED = (
        # The ruling's list.
        ["gh", "api", "-X", "PUT", "repos/owner/repo/pulls/42/merge"],
        ["gh", "api", "--method", "PUT", "repos/owner/repo/pulls/42/merge"],
        ["gh", "api", "-X", "POST", "repos/owner/repo/merges", "-f", "base=main", "-f", "head=task/x"],
        ["gh", "api", "graphql", "-f", "query=mutation { mergePullRequest(input: {}) { clientMutationId } }"],
        ["gh", "api", "-X", "DELETE", "repos/owner/repo/pulls/42"],
        ["gh", "api", "-X", "PATCH", "repos/other/repo/pulls/42", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/issues/42", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "https://api.github.com/repos/owner/repo/pulls/42", "-f", "body=x"],
        # Spellings that would slip past a naive match.
        ["gh", "api", "-X", "PATCH", "/repos/owner/repo/pulls/42", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42/", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42?merge=1", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42/../../merges", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42/merge", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo2/pulls/42", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/0", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/x", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "graphql", "-f", "body=x"],
        ["gh", "api", "--method", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=x"],
        ["gh", "api", "-XPATCH", "repos/owner/repo/pulls/42", "-f", "body=x"],
        ["gh", "api", "-X", "patch", "repos/owner/repo/pulls/42", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "-X", "PUT", "repos/owner/repo/pulls/42", "-f", "body=x"],
        # Methods, fields and flags the adapter never sends.
        ["gh", "api", "repos/owner/repo/pulls/42"],
        ["gh", "api", "-X", "GET", "repos/owner/repo/pulls/42"],
        ["gh", "api", "-X", "POST", "repos/owner/repo/pulls", "-f", "title=t"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "state=closed"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "base=main"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=a", "-f", "body=b"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-F", "body=@/etc/passwd"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "--input", "body.json"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=x", "--hostname", "h"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "repos/owner/repo/pulls/43", "-f", "body=x"],
        ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f"],
        ["gh", "api", "-X"],
        # Global flags before `api`, whatever the path to gh.
        ["gh", "--hostname", "h", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=x"],
        ["/usr/bin/gh", "-R", "owner/repo", "api", "-X", "PUT", "repos/owner/repo/pulls/42/merge"],
        ["./gh", "--repo=owner/repo", "api", "graphql", "-f", "query=x"],
    )

    def test_each_listed_adapter_form_is_admitted(self) -> None:
        for argv in self.ADMITTED:
            with self.subTest(argv=argv):
                assert_gh_api_allowed(argv, repo="owner/repo")

    def test_each_listed_other_form_is_refused(self) -> None:
        for argv in self.REFUSED:
            with self.subTest(argv=argv):
                with self.assertRaises(GitHubPrError):
                    assert_gh_api_allowed(argv, repo="owner/repo")

    def test_the_adapter_never_runs_a_refused_form(self) -> None:
        runner = FakeGhRunner()
        adapter = GitHubPrAdapter("owner/repo", runner=runner)
        for argv in self.REFUSED:
            with self.subTest(argv=argv):
                with self.assertRaises(GitHubPrError):
                    adapter.run(argv, cwd=Path("/tmp"))
        self.assertEqual(runner.calls, [])

    def test_non_api_gh_commands_are_left_to_the_merge_guard(self) -> None:
        for argv in (
            ["gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"],
            ["gh", "pr", "create", "--repo", "owner/repo", "--draft"],
            ["git", "push", "origin", "task/AT-101"],
        ):
            with self.subTest(argv=argv):
                assert_gh_api_allowed(argv, repo="owner/repo")

    def test_the_repository_must_be_exactly_the_adapters(self) -> None:
        argv = ["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42", "-f", "body=x"]
        for repo in ("other/repo", "owner/repo2", "owner", "owner/repo/extra"):
            with self.subTest(repo=repo):
                with self.assertRaises(GitHubPrError):
                    assert_gh_api_allowed(argv, repo=repo)


class MergeIsForbiddenTests(unittest.TestCase):
    def test_adapter_has_no_merge_method(self) -> None:
        public = [name for name in dir(GitHubPrAdapter) if not name.startswith("_")]
        self.assertEqual([name for name in public if "merge" in name.lower()], [])

    def test_merge_guard_rejects_each_listed_merge_argv(self) -> None:
        for argv in (
            ["gh", "pr", "merge", "42"],
            ["gh", "pr", "merge", "42", "--squash"],
            ["gh", "api", "-X", "PUT", "repos/owner/repo/pulls/42/merge"],
            # Ruling 35b: the branch-merge endpoint and GraphQL.
            ["gh", "api", "-X", "POST", "repos/owner/repo/merges", "-f", "base=main"],
            ["gh", "api", "graphql", "-f", "query=mutation { mergePullRequest }"],
            ["gh", "api", "https://api.github.com/graphql", "-f", "query=x"],
            ["git", "merge", "task/AT-101"],
            ["git", "push", "origin", "HEAD:main"],
        ):
            with self.assertRaises(GitHubPrError):
                assert_not_a_merge_command(argv)

    def test_merge_guard_allows_each_listed_non_merge_pr_command(self) -> None:
        for argv in (
            ["gh", "pr", "create", "--draft"],
            ["gh", "pr", "edit", "42", "--body", "x"],
            ["gh", "pr", "view", "42", "--json", "state"],
            ["gh", "pr", "view", "42", "--json", "merged,mergedAt,mergeCommit"],
            ["gh", "pr", "comment", "42", "--body", "merge"],
            ["/usr/bin/gh", "--repo", "o/r", "pr", "view", "42"],
            ["gh", "api", "repos/o/r/pulls/42"],
        ):
            with self.subTest(argv=argv):
                assert_not_a_merge_command(argv)

    def test_merge_guard_rejects_pr_merge_behind_each_listed_path_and_global_flag(self) -> None:
        """Review Ruling 3: the guard matches parsed argv, not a string prefix."""
        for argv in (
            ["/usr/bin/gh", "pr", "merge", "42"],
            ["./gh", "pr", "merge", "42"],
            ["gh", "--repo", "o/r", "pr", "merge", "42"],
            ["gh", "-R", "o/r", "pr", "merge", "42", "--squash"],
            ["gh", "--repo=o/r", "pr", "merge", "42"],
            ["gh", "--hostname", "github.example.com", "pr", "merge", "42"],
            ["/usr/local/bin/gh", "-R", "o/r", "pr", "merge", "--rebase", "42"],
            ["env", "gh", "pr", "merge", "42"],
            ["gh", "api", "-X", "PUT", "repos/o/r/pulls/42/merge"],
            ["/usr/bin/gh", "api", "--method", "PUT", "repos/o/r/pulls/42/merge"],
            ["/usr/bin/git", "-C", "/tmp/x", "merge", "origin/main"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(GitHubPrError):
                    assert_not_a_merge_command(argv)

    def test_adapter_refuses_to_run_a_merge_argv(self) -> None:
        adapter = GitHubPrAdapter("owner/repo", runner=FakeGhRunner())
        for argv in (
            ["gh", "pr", "merge", "42"],
            ["/usr/bin/gh", "--repo", "owner/repo", "pr", "merge", "42"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(GitHubPrError):
                    adapter.run(argv, cwd=Path("/tmp"))


if __name__ == "__main__":
    unittest.main()
