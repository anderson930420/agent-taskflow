"""Tests for agent_taskflow.integration_watcher (spec §25, §25.0, §32, §33, §35)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_schema as schema
from agent_taskflow import integration_git as git_ops
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_queue import queue_for_repo
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_watcher import (
    WatcherRequest,
    poll_pr_outcomes,
    poll_target_freshness,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from v1_step2_fixtures import FakeGhRunner, GitFixture  # noqa: E402


class WatcherTestCase(unittest.TestCase):
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

    def make_reviewing_task(self, task_key: str, *, pr_number: int = 42) -> Path:
        worktree = self.fixture.create_task_worktree(task_key)
        self.fixture.commit_in(worktree, f"{task_key}.txt", "f\n", "feature")
        git_ops.push_branch(
            worktree, remote="origin", branch=f"task/{task_key}", base_branch="main"
        )
        artifact_dir = self.artifacts / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
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
            pr_number=pr_number,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
            pr_state="open",
            pr_head_sha=git_ops.head_sha(worktree),
            integrated_base_sha=self.fixture.target_sha(),
        )
        self.gh_runner.set_pr(
            pr_number,
            state="OPEN",
            headRefName=f"task/{task_key}",
            baseRefName="main",
            headRefOid=git_ops.head_sha(worktree),
        )
        return worktree

    def request(self, **overrides) -> WatcherRequest:
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
            self.request(**overrides), store=self.store, integration_store=self.integration
        )

    def outcomes(self, **overrides):
        return poll_pr_outcomes(
            self.request(**overrides),
            store=self.store,
            integration_store=self.integration,
            github=self.github,
        )

    def status_of(self, task_key: str) -> str:
        return self.store.get_task(task_key).status


class TargetFreshnessTests(WatcherTestCase):
    def test_a_fresh_pr_is_left_alone(self) -> None:
        self.make_reviewing_task("AT-201")
        results = self.freshness()
        self.assertEqual(results[0].behind_count, 0)
        self.assertFalse(results[0].stale)
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_REVIEW)

    def test_an_advanced_target_makes_the_pr_stale(self) -> None:
        self.make_reviewing_task("AT-201")
        self.fixture.advance_target()
        results = self.freshness()
        self.assertGreater(results[0].behind_count, 0)
        self.assertTrue(results[0].stale)

    def test_a_stale_pr_returns_to_ready_for_integration(self) -> None:
        self.make_reviewing_task("AT-201")
        self.fixture.advance_target()
        self.freshness()
        self.assertEqual(self.status_of("AT-201"), schema.READY_FOR_INTEGRATION)
        self.assertIs(self.integration.get_pr_state("AT-201")["reintegration_required"], True)

    def test_a_stale_pr_is_requeued_for_its_repo(self) -> None:
        self.make_reviewing_task("AT-201")
        self.fixture.advance_target()
        self.freshness()
        self.assertEqual(
            [entry.task_key for entry in queue_for_repo(self.integration, "owner/repo")],
            ["AT-201"],
        )

    def test_stale_detection_saves_both_base_shas(self) -> None:
        self.make_reviewing_task("AT-201")
        previous = self.integration.get_pr_state("AT-201")["integrated_base_sha"]
        advanced = self.fixture.advance_target()
        self.freshness()
        private = self.integration.get_integration_state("AT-201")
        self.assertEqual(private["previous_integrated_base_sha"], previous)
        self.assertEqual(private["new_target_sha"], advanced)

    def test_only_needs_review_tickets_are_examined(self) -> None:
        self.make_reviewing_task("AT-201")
        self.store.update_task_status("AT-201", schema.NEEDS_DECISION, source="test")
        self.fixture.advance_target()
        self.assertEqual(self.freshness(), [])
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_DECISION)

    def test_freshness_polling_is_idempotent(self) -> None:
        self.make_reviewing_task("AT-201")
        self.fixture.advance_target()
        self.freshness()
        self.assertEqual(self.freshness(), [])
        self.assertEqual(
            [entry.task_key for entry in queue_for_repo(self.integration, "owner/repo")],
            ["AT-201"],
        )

    def test_an_in_progress_integration_is_not_interrupted(self) -> None:
        """§25.0 — a target that advances mid-integration gets no special handling."""
        self.make_reviewing_task("AT-201")
        self.store.update_task_status("AT-201", schema.READY_FOR_INTEGRATION, source="test")
        self.store.update_task_status("AT-201", schema.INTEGRATING, source="test")
        self.fixture.advance_target()
        self.assertEqual(self.freshness(), [])
        self.assertEqual(self.status_of("AT-201"), schema.INTEGRATING)

    def test_dry_run_freshness_polling_changes_nothing(self) -> None:
        self.make_reviewing_task("AT-201")
        self.fixture.advance_target()
        results = self.freshness(confirm_poll=False)
        self.assertTrue(results[0].stale)
        self.assertFalse(results[0].requeued)
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_REVIEW)


class PrOutcomeTests(WatcherTestCase):
    def test_polling_records_the_spec_pr_fields(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, reviewDecision="APPROVED", statusCheckRollup=[{"state": "SUCCESS"}])
        self.outcomes()
        state = self.integration.get_pr_state("AT-201")
        self.assertEqual(state["pr_state"], "open")
        self.assertEqual(state["review_decision"], "approved")
        self.assertEqual(state["ci_status"], "success")
        self.assertIsNotNone(state["pr_last_polled_at"])

    def test_approved_but_unmerged_stays_in_needs_review(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, reviewDecision="APPROVED")
        self.outcomes()
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_REVIEW)

    def test_red_ci_never_mutates_the_lifecycle(self) -> None:
        """§30 — GitHub CI is not a Taskflow lifecycle authority."""
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, statusCheckRollup=[{"state": "FAILURE"}])
        self.outcomes()
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_REVIEW)
        self.assertEqual(self.integration.get_pr_state("AT-201")["ci_status"], "failure")

    def test_changes_requested_moves_to_needs_decision(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(
            42,
            reviewDecision="CHANGES_REQUESTED",
            reviews=[
                {
                    "author": {"login": "octocat"},
                    "state": "CHANGES_REQUESTED",
                    "body": "please split this",
                    "submittedAt": "2026-09-10T03:00:00Z",
                }
            ],
        )
        self.outcomes()
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_DECISION)

    def test_changes_requested_persists_retry_context(self) -> None:
        self.make_reviewing_task("AT-201")
        head = self.integration.get_pr_state("AT-201")["pr_head_sha"]
        self.gh_runner.set_pr(
            42,
            reviewDecision="CHANGES_REQUESTED",
            reviews=[
                {
                    "author": {"login": "octocat"},
                    "state": "CHANGES_REQUESTED",
                    "body": "please split this",
                    "submittedAt": "2026-09-10T03:00:00Z",
                }
            ],
        )
        self.outcomes()
        row = self.integration.list_review_evidence("AT-201")[0]
        self.assertEqual(row["reviewer"], "octocat")
        self.assertEqual(row["reviewed_at"], "2026-09-10T03:00:00Z")
        self.assertEqual(row["reviewed_head_sha"], head)
        self.assertEqual(row["pr_url"], "https://github.com/owner/repo/pull/42")
        self.assertEqual(row["comments"][0]["body"], "please split this")

    def test_taskflow_never_transitions_needs_decision_back_by_itself(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, reviewDecision="CHANGES_REQUESTED")
        self.outcomes()
        self.gh_runner.set_pr(42, reviewDecision="APPROVED")
        self.outcomes()
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_DECISION)

    def test_pr_closed_unmerged_cancels_the_ticket(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, state="CLOSED", merged=False)
        self.outcomes()
        self.assertEqual(self.status_of("AT-201"), schema.CANCELLED)

    def test_pr_closed_unmerged_retains_the_workspace_and_evidence(self) -> None:
        worktree = self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, state="CLOSED", merged=False)
        result = self.outcomes()[0]
        self.assertTrue(worktree.is_dir())
        self.assertIs(result.cleanup_performed, False)
        from v1_step2_fixtures import git as raw_git

        self.assertIn("task/AT-201", raw_git(self.fixture.repo, "branch", "--list", "task/AT-201"))

    def test_merge_is_detected_and_recorded(self) -> None:
        worktree = self.make_reviewing_task("AT-201")
        merge_sha = self.fixture.merge_branch_into_target("task/AT-201")
        self.gh_runner.set_pr(
            42,
            state="MERGED",
            merged=True,
            mergedAt="2026-09-10T04:00:00Z",
            mergeCommit={"oid": merge_sha},
        )
        result = self.outcomes()[0]
        self.assertTrue(result.merged)
        state = self.integration.get_pr_state("AT-201")
        self.assertIs(state["pr_merged"], True)
        self.assertEqual(state["merge_commit_sha"], merge_sha)

    def test_merge_detection_does_not_complete_or_clean_up_by_itself(self) -> None:
        worktree = self.make_reviewing_task("AT-201")
        merge_sha = self.fixture.merge_branch_into_target("task/AT-201")
        self.gh_runner.set_pr(
            42, state="MERGED", merged=True, mergedAt="x", mergeCommit={"oid": merge_sha}
        )
        self.outcomes()
        self.assertNotEqual(self.status_of("AT-201"), schema.COMPLETED)
        self.assertTrue(worktree.is_dir())

    def test_pr_polling_is_idempotent(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(
            42,
            reviewDecision="CHANGES_REQUESTED",
            reviews=[{"author": {"login": "octocat"}, "state": "CHANGES_REQUESTED", "body": "x", "submittedAt": "t"}],
        )
        self.outcomes()
        self.outcomes()
        self.outcomes()
        self.assertEqual(len(self.integration.list_review_evidence("AT-201")), 1)
        statuses = [
            entry
            for entry in self.store.list_task_events("AT-201")
            if entry.event_type == "status_changed"
        ]
        self.assertEqual(len([s for s in statuses if "needs_decision" in (s.payload_json or "")]), 1)

    def test_dry_run_pr_polling_changes_nothing(self) -> None:
        self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, state="CLOSED", merged=False)
        results = self.outcomes(confirm_poll=False)
        self.assertEqual(results[0].proposed_transition, schema.CANCELLED)
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_pr_state("AT-201")["pr_last_polled_at"])

    def test_tickets_without_a_pr_are_skipped(self) -> None:
        self.make_reviewing_task("AT-201")
        self.integration.update_pr_state("AT-201", pr_number=None)
        self.assertEqual(self.outcomes(), [])


if __name__ == "__main__":
    unittest.main()
