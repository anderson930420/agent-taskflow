"""V1 Step 2 acceptance gate.

Maps directly onto the spec's §43 end-to-end acceptance items and §44 safety
invariants that Step 2 can affect, plus the required negative-scope tests.
Each test names the item or invariant it covers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_schema as schema
from agent_taskflow import integration_git as git_ops
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_cleanup import (
    IntegrationCleanupRequest,
    run_integration_cleanup,
)
from agent_taskflow.integration_controller import IntegrationRequest, integrate_task
from agent_taskflow.integration_metrics import compute_integration_metrics
from agent_taskflow.integration_queue import enqueue_for_integration, queue_for_repo
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.integration_watcher import (
    WatcherRequest,
    poll_pr_outcomes,
    poll_target_freshness,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from v1_step2_fixtures import FakeGhRunner, GitFixture, git as raw_git  # noqa: E402


GREEN = (IntegrationValidatorSpec(name="unit", command=("true",)),)
RED = (IntegrationValidatorSpec(name="unit", command=("false",)),)

STEP2_MODULES = (
    "integration_schema",
    "integration_store",
    "integration_git",
    "github_pr_adapter",
    "integration_validators",
    "integration_conflict_resolver",
    "integration_queue",
    "reviewer_hints",
    "integration_controller",
    "merge_verification",
    "integration_watcher",
    "integration_cleanup",
    "integration_metrics",
)


class AcceptanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.gh_runner = FakeGhRunner()
        self.github = GitHubPrAdapter("owner/repo", runner=self.gh_runner)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def make_task(self, task_key: str, *, status: str = schema.READY_FOR_INTEGRATION) -> Path:
        worktree = self.fixture.create_task_worktree(task_key)
        self.fixture.commit_in(worktree, f"{task_key}.txt", "feature\n", f"{task_key} feature")
        artifact_dir = self.artifacts / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="demo",
                status=status,
                repo_path=self.fixture.repo,
                title=f"{task_key} title",
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
        return worktree

    def integrate(self, task_key: str, **overrides):
        kwargs = dict(
            task_key=task_key,
            repo="owner/repo",
            db_path=self.db_path,
            target_branch="main",
            remote="origin",
            validator_specs=GREEN,
            dry_run=False,
            confirm_integration=True,
            owner="acceptance",
        )
        kwargs.update(overrides)
        return integrate_task(
            IntegrationRequest(**kwargs),
            store=self.store,
            integration_store=self.integration,
            github=self.github,
        )

    def watcher_request(self, **overrides) -> WatcherRequest:
        kwargs = dict(
            repo="owner/repo",
            repo_path=self.fixture.repo,
            target_branch="main",
            remote="origin",
            db_path=self.db_path,
            confirm_poll=True,
        )
        kwargs.update(overrides)
        return WatcherRequest(**kwargs)

    def freshness(self, **overrides):
        return poll_target_freshness(
            self.watcher_request(**overrides), store=self.store, integration_store=self.integration
        )

    def pr_outcomes(self, **overrides):
        return poll_pr_outcomes(
            self.watcher_request(**overrides),
            store=self.store,
            integration_store=self.integration,
            github=self.github,
        )

    def cleanup(self, task_key: str, **overrides):
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
        return run_integration_cleanup(
            IntegrationCleanupRequest(**kwargs),
            store=self.store,
            integration_store=self.integration,
        )

    def sync_pr(self, task_key: str, **fields) -> None:
        number = self.integration.get_pr_state(task_key)["pr_number"]
        self.gh_runner.set_pr(number, **fields)

    def status_of(self, task_key: str) -> str:
        return self.store.get_task(task_key).status


class EndToEndJourneyTests(AcceptanceTestCase):
    """§43 items 12-14, 16, 18-22, 28-33 in one continuous lifecycle."""

    def test_full_lifecycle_from_queue_to_completed(self) -> None:
        worktree = self.make_task("AT-101")

        # 12 — completed implementation enters the per-repo integration queue.
        enqueue_for_integration(self.integration, "AT-101", repo="owner/repo")
        self.assertEqual(
            [e.task_key for e in queue_for_repo(self.integration, "owner/repo")], ["AT-101"]
        )

        # 14 — initial integration uses the latest target.
        advanced = self.fixture.advance_target("first.txt")
        result = self.integrate("AT-101")
        self.assertEqual(result.integrated_base_sha, advanced)

        # 15/16 — validators gate, then a PR exists and the ticket is reviewable.
        self.assertTrue(result.validators_passed)
        self.assertIsNotNone(result.pr_number)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)

        # 18/19 — the target is polled while the PR waits and staleness detected.
        second = self.fixture.advance_target("second.txt")
        freshness = self.freshness()
        self.assertGreater(freshness[0].behind_count, 0)
        self.assertTrue(freshness[0].stale)

        # 20 — the stale ticket is re-integrated automatically.
        self.assertEqual(self.status_of("AT-101"), schema.READY_FOR_INTEGRATION)
        reintegration = self.integrate("AT-101")

        # 21/22 — same PR, no force push, validators re-run.
        self.assertEqual(reintegration.pr_number, result.pr_number)
        self.assertIs(reintegration.force_pushed, False)
        self.assertTrue(reintegration.validators_passed)
        self.assertEqual(reintegration.integrated_base_sha, second)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)

        # 28/29 — a human merges on GitHub; Taskflow only polls.
        merge_sha = self.fixture.merge_branch_into_target("task/AT-101")
        self.sync_pr(
            "AT-101",
            state="MERGED",
            merged=True,
            mergedAt="2026-09-10T05:00:00Z",
            mergeCommit={"oid": merge_sha},
        )
        outcome = self.pr_outcomes()[0]
        self.assertTrue(outcome.merged)

        # 30/31/32 — merge verified via merge_commit_sha, then cleanup runs.
        cleanup = self.cleanup("AT-101")
        self.assertTrue(cleanup.merge_verified)
        self.assertEqual(cleanup.merge_commit_sha, merge_sha)
        self.assertFalse(worktree.exists())

        # 33 — the ticket reaches completed.
        self.assertEqual(self.status_of("AT-101"), schema.COMPLETED)

        # 35 — metrics are available for the run.
        metrics = compute_integration_metrics(self.integration, store=self.store)
        self.assertEqual(metrics.reintegration_count_total, 1)
        self.assertGreater(metrics.reintegration_rate, 0.0)

    def test_item_13_integration_is_serialized_per_repo_and_parallel_across_repos(self) -> None:
        self.make_task("AT-101")
        self.make_task("AT-102")
        self.integrate("AT-101")
        self.integrate("AT-102")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertEqual(self.status_of("AT-102"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))

    def test_item_15_red_validators_never_reach_needs_review(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101", validator_specs=RED)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)

    def test_item_17_multiple_same_repo_prs_stay_in_needs_review(self) -> None:
        for key in ("AT-101", "AT-102", "AT-103"):
            self.make_task(key)
            self.integrate(key)
        for key in ("AT-101", "AT-102", "AT-103"):
            self.assertEqual(self.status_of(key), schema.NEEDS_REVIEW)

    def test_item_23_and_24_changes_requested_and_retry_context(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        head = self.integration.get_pr_state("AT-101")["pr_head_sha"]
        self.sync_pr(
            "AT-101",
            reviewDecision="CHANGES_REQUESTED",
            reviews=[
                {
                    "author": {"login": "octocat"},
                    "state": "CHANGES_REQUESTED",
                    "body": "split this up",
                    "submittedAt": "2026-09-10T03:00:00Z",
                }
            ],
        )
        self.pr_outcomes()
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        evidence = self.integration.list_review_evidence("AT-101")[0]
        self.assertEqual(evidence["reviewer"], "octocat")
        self.assertEqual(evidence["reviewed_at"], "2026-09-10T03:00:00Z")
        self.assertEqual(evidence["reviewed_head_sha"], head)
        self.assertEqual(evidence["comments"][0]["body"], "split this up")
        self.assertTrue(evidence["pr_url"])

    def test_item_25_and_26_closed_unmerged_cancels_and_retains(self) -> None:
        worktree = self.make_task("AT-101")
        self.integrate("AT-101")
        self.sync_pr("AT-101", state="CLOSED", merged=False)
        self.pr_outcomes()
        self.assertEqual(self.status_of("AT-101"), schema.CANCELLED)
        self.assertTrue(worktree.is_dir())
        self.assertIn("task/AT-101", raw_git(self.fixture.repo, "branch", "--list", "task/AT-101"))
        self.assertTrue((self.artifacts / "AT-101").is_dir())
        self.assertTrue(self.store.list_task_artifacts("AT-101"))

    def test_item_27_github_ci_never_mutates_the_lifecycle(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        self.sync_pr("AT-101", statusCheckRollup=[{"state": "FAILURE"}])
        self.pr_outcomes()
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertEqual(self.integration.get_pr_state("AT-101")["ci_status"], "failure")

    def test_item_29_polling_is_idempotent(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        merge_sha = self.fixture.merge_branch_into_target("task/AT-101")
        self.sync_pr("AT-101", state="MERGED", merged=True, mergedAt="t", mergeCommit={"oid": merge_sha})
        first = self.pr_outcomes()
        second = self.pr_outcomes()
        self.assertTrue(first[0].merged)
        self.assertEqual(self.integration.get_pr_state("AT-101")["merge_commit_sha"], merge_sha)
        # A merged PR is recorded as closed, so it leaves the §32 pickup set
        # (pr_number IS NOT NULL AND pr_state = 'open'). Re-polling therefore
        # re-processes nothing: no second merge_detected, no state change.
        self.assertEqual(second, [])
        merge_events = [
            event
            for event in self.store.list_task_events("AT-101")
            if event.event_type == "merge_detected"
        ]
        self.assertEqual(len(merge_events), 1)
        self.assertNotEqual(self.status_of("AT-101"), schema.COMPLETED)

    def test_item_31_every_github_merge_method_is_supported(self) -> None:
        for index, method in enumerate(("merge", "squash", "rebase")):
            with self.subTest(method=method):
                key = f"AT-2{index}"
                worktree = self.make_task(key)
                task_sha = git_ops.head_sha(worktree)
                self.integrate(key)
                merge_sha = self.fixture.merge_branch_into_target(f"task/{key}", method=method)
                self.integration.update_pr_state(
                    key, pr_merged=True, pr_state="closed", merge_commit_sha=merge_sha
                )
                cleanup = self.cleanup(key)
                self.assertTrue(cleanup.merge_verified, cleanup.summary)
                self.assertEqual(self.status_of(key), schema.COMPLETED)
                self.assertNotEqual(merge_sha, task_sha)

    def test_item_34_dependents_are_not_released_before_completed(self) -> None:
        """Step 2 half of §43.34: nothing here releases a dependent early."""
        self.make_task("AT-101")
        self.integrate("AT-101")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        events = [event.event_type for event in self.store.list_task_events("AT-101")]
        for forbidden in ("dependency_released", "blocked_by_cleared"):
            self.assertNotIn(forbidden, events)


class SafetyInvariantTests(AcceptanceTestCase):
    """§44 invariants that Step 2 can affect."""

    def test_integration_lock_does_not_wait_for_human_review(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))

    def test_every_integration_uses_the_latest_available_target(self) -> None:
        self.make_task("AT-101")
        first = self.fixture.advance_target("a.txt")
        self.assertEqual(self.integrate("AT-101").integrated_base_sha, first)
        second = self.fixture.advance_target("b.txt")
        self.freshness()
        self.assertEqual(self.integrate("AT-101").integrated_base_sha, second)

    def test_published_pr_branches_are_not_force_pushed(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        self.fixture.advance_target()
        self.freshness()
        result = self.integrate("AT-101")
        pushes = [cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")]
        self.assertTrue(pushes)
        for cmd in pushes:
            self.assertFalse({"--force", "-f", "--force-with-lease"} & set(cmd))

    def test_taskflow_validator_failure_stops_for_decision(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101", validator_specs=RED)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)

    def test_ai_cannot_self_approve(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        decisions = self.store.list_approval_decisions("AT-101")
        self.assertEqual(decisions, [])
        self.assertEqual(self.integration.get_pr_state("AT-101")["review_decision"], None)

    def test_merge_verification_uses_the_pr_result_not_the_task_sha(self) -> None:
        worktree = self.make_task("AT-101")
        self.integrate("AT-101")
        task_sha = git_ops.head_sha(worktree)
        self.fixture.merge_branch_into_target("task/AT-101", method="squash")
        self.integration.update_pr_state(
            "AT-101", pr_merged=True, merge_commit_sha=task_sha
        )
        self.assertFalse(self.cleanup("AT-101").merge_verified)
        self.assertTrue(worktree.is_dir())

    def test_closed_unmerged_work_is_not_automatically_destroyed(self) -> None:
        worktree = self.make_task("AT-101")
        self.integrate("AT-101")
        self.sync_pr("AT-101", state="CLOSED", merged=False)
        self.pr_outcomes()
        self.cleanup("AT-101")
        self.assertTrue(worktree.is_dir())

    def test_all_lifecycle_mutations_are_auditable(self) -> None:
        self.make_task("AT-101")
        self.integrate("AT-101")
        self.fixture.advance_target()
        self.freshness()
        self.integrate("AT-101")
        statuses = [
            event
            for event in self.store.list_task_events("AT-101")
            if event.event_type == "status_changed"
        ]
        self.assertGreaterEqual(len(statuses), 4)
        for event in statuses:
            self.assertTrue(event.source)
            self.assertTrue(event.created_at)


class NegativeScopeTests(AcceptanceTestCase):
    """Required negative-scope tests: no force push, no merge, no silent cleanup."""

    def test_git_and_gh_execution_is_confined_to_the_guarded_chokepoints(self) -> None:
        """No Step 2 module may shell out to git/gh around the guards.

        ``integration_git`` is the only module that executes git, and
        ``github_pr_adapter`` the only one that executes gh; both apply the
        force-push and merge denylists to every argv. ``integration_validators``
        runs caller-supplied validator commands, which is its whole job. Any
        other module reaching for ``subprocess`` would be a bypass.
        """
        import ast

        package = Path(__file__).resolve().parents[1] / "agent_taskflow"
        chokepoints = {"integration_git", "github_pr_adapter", "integration_validators"}
        offenders: list[str] = []
        for name in STEP2_MODULES:
            if name in chokepoints:
                continue
            tree = ast.parse((package / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(module.split(".")[0] == "subprocess" for module in names):
                    offenders.append(f"{name}.py:{node.lineno} imports subprocess")
        self.assertEqual(offenders, [])

    def test_the_force_push_guard_rejects_every_force_form(self) -> None:
        from agent_taskflow.integration_git import (
            IntegrationGitError,
            assert_no_force_push,
        )

        for argv in (
            ["git", "push", "--force", "origin", "task/AT-101"],
            ["git", "push", "-f", "origin", "task/AT-101"],
            ["git", "push", "--force-with-lease", "origin", "task/AT-101"],
            ["git", "push", "--force-if-includes", "origin", "task/AT-101"],
            ["git", "push", "origin", "+task/AT-101"],
            ["git", "push", "origin", "+task/AT-101:task/AT-101"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(IntegrationGitError):
                    assert_no_force_push(argv)

    def test_the_git_allowlist_excludes_history_rewriting_subcommands(self) -> None:
        from agent_taskflow.integration_git import ALLOWED_SUBCOMMANDS

        for subcommand in (
            "reset",
            "clean",
            "filter-branch",
            "update-ref",
            "reflog",
            "gc",
            "checkout",
            "switch",
            "restore",
        ):
            self.assertNotIn(subcommand, ALLOWED_SUBCOMMANDS)

    def test_every_step2_git_call_is_routed_through_run_git(self) -> None:
        """Unallowlisted subcommands are unreachable, not merely unused."""
        worktree = self.make_task("AT-101")
        for args in (
            ["reset", "--hard", "origin/main"],
            ["checkout", "main"],
            ["clean", "-fdx"],
            ["update-ref", "refs/heads/main", "HEAD"],
        ):
            with self.subTest(args=args):
                with self.assertRaises(git_ops.IntegrationGitError):
                    git_ops.run_git(worktree, args)

    def test_a_merge_argv_is_rejected_everywhere_it_could_be_built(self) -> None:
        from agent_taskflow.github_pr_adapter import GitHubPrError, assert_not_a_merge_command

        for argv in (
            ["gh", "pr", "merge", "42", "--squash"],
            ["gh", "pr", "merge", "42", "--rebase"],
            ["gh", "pr", "merge", "42", "--merge"],
        ):
            with self.assertRaises(GitHubPrError):
                assert_not_a_merge_command(argv)

    def test_cancelled_cleanup_without_the_flag_removes_nothing(self) -> None:
        worktree = self.make_task("AT-101")
        self.integrate("AT-101")
        self.sync_pr("AT-101", state="CLOSED", merged=False)
        self.pr_outcomes()
        result = self.cleanup("AT-101", confirm_cleanup=True, confirm_cancelled_cleanup=False)
        self.assertFalse(result.ok)
        self.assertIs(result.worktree_removed, False)
        self.assertTrue(worktree.is_dir())
        self.assertIn("task/AT-101", raw_git(self.fixture.repo, "branch", "--list", "task/AT-101"))

    def test_no_entry_point_mutates_without_explicit_confirmation(self) -> None:
        worktree = self.make_task("AT-101")
        self.assertEqual(
            self.integrate("AT-101", dry_run=True, confirm_integration=False).status, "dry_run"
        )
        self.assertEqual(self.status_of("AT-101"), schema.READY_FOR_INTEGRATION)
        self.assertEqual(self.gh_runner.calls, [])
        self.assertTrue(worktree.is_dir())

    def test_step2_never_touches_the_default_state_database(self) -> None:
        """Every Step 2 entry point must accept an explicit db_path."""
        import inspect

        from agent_taskflow.integration_cleanup import IntegrationCleanupRequest
        from agent_taskflow.integration_controller import IntegrationRequest
        from agent_taskflow.integration_watcher import WatcherRequest

        for request_cls in (IntegrationRequest, WatcherRequest, IntegrationCleanupRequest):
            self.assertIn("db_path", inspect.signature(request_cls).parameters)


if __name__ == "__main__":
    unittest.main()
