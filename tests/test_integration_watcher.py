"""Tests for agent_taskflow.integration_watcher (spec §25, §25.0, §32, §33, §35)."""

from __future__ import annotations

from dataclasses import asdict
import json
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
from agent_taskflow.integration_queue import enqueue_for_integration, queue_for_repo
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

    def test_only_needs_review_tickets_are_requeued(self) -> None:
        """Picked up by the §32.0 condition, but needs_decision waits for a human."""
        self.make_reviewing_task("AT-201")
        self.store.update_task_status("AT-201", schema.NEEDS_DECISION, source="test")
        self.fixture.advance_target()
        results = self.freshness()
        self.assertEqual([(r.task_key, r.stale, r.requeued) for r in results], [("AT-201", True, False)])
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_DECISION)
        self.assertEqual(queue_for_repo(self.integration, "owner/repo"), [])
    def test_freshness_polling_is_idempotent(self) -> None:
        self.make_reviewing_task("AT-201")
        self.fixture.advance_target()
        self.freshness()
        # Still picked up (its PR is open), but now ready_for_integration, so
        # a second tick re-queues nothing and the queue is unchanged.
        second = self.freshness()
        self.assertEqual([r.requeued for r in second], [False])
        self.assertEqual(
            [entry.task_key for entry in queue_for_repo(self.integration, "owner/repo")],
            ["AT-201"],
        )
    def test_an_in_progress_integration_is_not_interrupted(self) -> None:
        """§25.0 — picked up but deferred: no git work, no re-queue."""
        self.make_reviewing_task("AT-201")
        self.store.update_task_status("AT-201", schema.READY_FOR_INTEGRATION, source="test")
        self.store.update_task_status("AT-201", schema.INTEGRATING, source="test")
        self.fixture.advance_target()
        results = self.freshness()
        self.assertEqual([(r.task_key, r.deferred) for r in results], [("AT-201", True)])
        self.assertEqual(self.status_of("AT-201"), schema.INTEGRATING)
        self.assertEqual(queue_for_repo(self.integration, "owner/repo"), [])
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
        self.gh_runner.set_pr(42, state="CLOSED")
        self.outcomes()
        self.assertEqual(self.status_of("AT-201"), schema.CANCELLED)

    def test_pr_closed_unmerged_retains_the_workspace_and_evidence(self) -> None:
        worktree = self.make_reviewing_task("AT-201")
        self.gh_runner.set_pr(42, state="CLOSED")
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
            42, state="MERGED", mergedAt="x", mergeCommit={"oid": merge_sha}
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
        self.gh_runner.set_pr(42, state="CLOSED")
        results = self.outcomes(confirm_poll=False)
        self.assertEqual(results[0].proposed_transition, schema.CANCELLED)
        self.assertEqual(self.status_of("AT-201"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_pr_state("AT-201")["pr_last_polled_at"])

    def test_tickets_without_a_pr_are_skipped(self) -> None:
        self.make_reviewing_task("AT-201")
        self.integration.update_pr_state("AT-201", pr_number=None)
        self.assertEqual(self.outcomes(), [])



class PollFailureTests(WatcherTestCase):
    """Review round 4, Ruling 29e — a failed poll is never swallowed. It is
    audited with the original exception and stops the Ticket for a decision
    (§27.2.1) wherever the transition table lets integration move it there."""

    def fail_polls_with(self, exc: BaseException) -> None:
        def runner(args, **kwargs):
            raise exc

        self.github = GitHubPrAdapter("owner/repo", runner=runner)

    def failed_events(self, task_key: str) -> list[dict]:
        return [
            json.loads(e.payload_json)
            for e in self.store.list_task_events(task_key)
            if e.event_type == "pr_poll_failed"
        ]

    def test_a_gh_error_stops_a_reviewing_ticket_for_decision(self) -> None:
        self.make_reviewing_task("AT-801")
        self.gh_runner.pulls.pop(42)  # gh now answers "no such PR", rc=1
        [outcome] = self.outcomes()
        self.assertEqual(outcome.applied_transition, schema.NEEDS_DECISION)
        self.assertIn("GitHubPrError", outcome.poll_error)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        [event] = self.failed_events("AT-801")
        self.assertEqual(event["exception_type"], "GitHubPrError")
        self.assertIn("no such PR", event["exception_message"])
        self.assertEqual(event["pr_number"], 42)

    def test_the_old_unknown_field_failure_is_no_longer_silent(self) -> None:
        # What real gh did to every poll before Ruling 29a.
        self.make_reviewing_task("AT-801")

        def runner(args, **kwargs):
            from v1_step2_fixtures import FakeCompletedProcess, unknown_gh_json_field_error

            return FakeCompletedProcess(returncode=1, stderr=unknown_gh_json_field_error("merged"))

        self.github = GitHubPrAdapter("owner/repo", runner=runner)
        [outcome] = self.outcomes()
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertIn('Unknown JSON field: "merged"', self.failed_events("AT-801")[0]["exception_message"])

    def test_a_missing_gh_binary_stops_the_ticket_for_decision(self) -> None:
        self.make_reviewing_task("AT-801")
        self.fail_polls_with(FileNotFoundError(2, "No such file or directory", "gh"))
        [outcome] = self.outcomes()
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertEqual(self.failed_events("AT-801")[0]["exception_type"], "FileNotFoundError")

    def test_an_undecodable_response_stops_the_ticket_for_decision(self) -> None:
        self.make_reviewing_task("AT-801")
        self.fail_polls_with(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"))
        self.outcomes()
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertEqual(self.failed_events("AT-801")[0]["exception_type"], "UnicodeDecodeError")

    def test_a_failed_poll_records_no_pr_fields(self) -> None:
        self.make_reviewing_task("AT-801")
        before = self.integration.get_pr_state("AT-801")
        self.fail_polls_with(RuntimeError("simulated"))
        self.outcomes()
        after = self.integration.get_pr_state("AT-801")
        self.assertEqual(after["pr_state"], "open")
        self.assertEqual(after["pr_last_polled_at"], before["pr_last_polled_at"])

    def test_a_paused_ticket_stays_paused_but_the_failure_is_audited(self) -> None:
        self.make_reviewing_task("AT-801")
        self.store.update_task_status("AT-801", schema.PAUSED, source="test")
        self.fail_polls_with(RuntimeError("simulated"))
        [outcome] = self.outcomes()
        self.assertIsNone(outcome.applied_transition)
        self.assertEqual(self.status_of("AT-801"), schema.PAUSED)
        self.assertEqual(len(self.failed_events("AT-801")), 1)

    def test_a_queued_ticket_keeps_its_queue_slot_but_the_failure_is_audited(self) -> None:
        self.make_reviewing_task("AT-801")
        self.store.update_task_status("AT-801", schema.READY_FOR_INTEGRATION, source="test")
        self.fail_polls_with(RuntimeError("simulated"))
        self.outcomes()
        self.assertEqual(self.status_of("AT-801"), schema.READY_FOR_INTEGRATION)
        self.assertEqual(len(self.failed_events("AT-801")), 1)

    def test_an_unconfirmed_poll_reports_the_failure_but_writes_nothing(self) -> None:
        self.make_reviewing_task("AT-801")
        self.fail_polls_with(RuntimeError("simulated"))
        [outcome] = self.outcomes(confirm_poll=False)
        self.assertEqual(outcome.poll_error, "RuntimeError: simulated")
        self.assertEqual(outcome.proposed_transition, schema.NEEDS_DECISION)
        self.assertIsNone(outcome.applied_transition)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        self.assertEqual(self.failed_events("AT-801"), [])

    def test_one_failing_poll_does_not_stop_the_others(self) -> None:
        self.make_reviewing_task("AT-801", pr_number=42)
        self.make_reviewing_task("AT-802", pr_number=43)
        self.gh_runner.pulls.pop(42)
        self.outcomes()
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertEqual(self.status_of("AT-802"), schema.NEEDS_REVIEW)
        self.assertEqual(self.failed_events("AT-802"), [])


class PrPickupScopeTests(WatcherTestCase):
    """§32 pickup scope — human ruling: every Ticket with an open PR.

    The pickup condition is ``pr_number IS NOT NULL AND pr_state = 'open'``,
    and it is deliberately not scoped by status. A human can merge a PR on
    GitHub while its Ticket sits in needs_decision or paused. A watcher scoped
    to needs_review would never see that merge, so the Ticket would never reach
    completed and its dependents would never be released. §32 says "all active
    PR Tickets", and these tests pin what that means.
    """

    def cleanup(self, task_key: str):
        return run_integration_cleanup(
            IntegrationCleanupRequest(
                task_key=task_key,
                repo="owner/repo",
                repo_path=self.fixture.repo,
                target_branch="main",
                remote="origin",
                db_path=self.db_path,
                confirm_cleanup=True,
            ),
            store=self.store,
            integration_store=self.integration,
        )

    def merge_on_github(self, task_key: str, *, pr_number: int = 42) -> str:
        merge_sha = self.fixture.merge_branch_into_target(f"task/{task_key}")
        self.gh_runner.set_pr(
            pr_number,
            state="MERGED",
            mergedAt="2026-09-10T05:00:00Z",
            mergeCommit={"oid": merge_sha},
        )
        return merge_sha

    def assert_merge_lands(self, task_key: str, parked_status: str) -> None:
        worktree = self.make_reviewing_task(task_key)
        self.store.update_task_status(task_key, parked_status, source="test")
        merge_sha = self.merge_on_github(task_key)

        picked = self.outcomes()
        self.assertEqual([o.task_key for o in picked], [task_key])
        self.assertTrue(picked[0].merged)
        self.assertEqual(
            self.integration.get_pr_state(task_key)["merge_commit_sha"], merge_sha
        )
        # Detection alone never completes a Ticket; §36 verification does.
        self.assertEqual(self.status_of(task_key), parked_status)

        result = self.cleanup(task_key)
        self.assertTrue(result.ok, result.summary)
        self.assertTrue(result.merge_verified)
        self.assertEqual(result.merge_commit_sha, merge_sha)
        self.assertEqual(self.status_of(task_key), schema.COMPLETED)
        self.assertFalse(worktree.exists())

    def test_a_needs_decision_ticket_whose_pr_is_merged_reaches_completed(self) -> None:
        self.assert_merge_lands("AT-401", schema.NEEDS_DECISION)

    def test_a_paused_ticket_whose_pr_is_merged_reaches_completed(self) -> None:
        self.assert_merge_lands("AT-402", schema.PAUSED)

    def test_a_ticket_with_a_null_pr_number_is_not_picked_up(self) -> None:
        self.make_reviewing_task("AT-403")
        self.integration.update_pr_state("AT-403", pr_number=None)
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(self.gh_runner.calls, [])

    def test_a_ticket_whose_pr_state_is_closed_is_not_picked_up(self) -> None:
        self.make_reviewing_task("AT-404")
        self.integration.update_pr_state("AT-404", pr_state="closed")
        self.gh_runner.set_pr(42, state="MERGED", mergeCommit={"oid": "x"})
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual(self.status_of("AT-404"), schema.NEEDS_REVIEW)

    def test_pickup_is_not_scoped_by_status(self) -> None:
        parked = {
            "AT-410": schema.NEEDS_REVIEW,
            "AT-411": schema.NEEDS_DECISION,
            "AT-412": schema.PAUSED,
            "AT-413": schema.READY_FOR_INTEGRATION,
        }
        for number, (key, status) in enumerate(parked.items(), start=50):
            self.make_reviewing_task(key, pr_number=number)
            self.store.update_task_status(key, status, source="test")
        self.assertEqual({o.task_key for o in self.outcomes()}, set(parked))

    def test_an_in_flight_integration_is_picked_up_but_not_polled(self) -> None:
        """§25.0 — nothing is polled, recorded or transitioned mid-integration."""
        self.make_reviewing_task("AT-405")
        self.store.update_task_status("AT-405", schema.INTEGRATING, source="test")
        self.merge_on_github("AT-405")
        outcomes = self.outcomes()
        self.assertEqual([o.task_key for o in outcomes], ["AT-405"])
        self.assertTrue(outcomes[0].deferred)
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual(self.integration.get_pr_state("AT-405")["pr_state"], "open")
        self.assertEqual(self.status_of("AT-405"), schema.INTEGRATING)

    def test_changes_requested_on_a_paused_ticket_keeps_it_paused(self) -> None:
        """The review is kept as retry context, but a pause is the user's call."""
        self.make_reviewing_task("AT-406")
        self.store.update_task_status("AT-406", schema.PAUSED, source="test")
        self.gh_runner.set_pr(
            42,
            reviewDecision="CHANGES_REQUESTED",
            reviews=[
                {
                    "author": {"login": "octocat"},
                    "state": "CHANGES_REQUESTED",
                    "body": "split this",
                    "submittedAt": "2026-09-10T03:00:00Z",
                }
            ],
        )
        self.outcomes()
        self.assertEqual(self.status_of("AT-406"), schema.PAUSED)
        self.assertEqual(len(self.integration.list_review_evidence("AT-406")), 1)

    def test_a_pr_closed_unmerged_while_needs_decision_cancels_and_retains(self) -> None:
        """§33.5 applies whatever status the Ticket was waiting in."""
        worktree = self.make_reviewing_task("AT-407")
        self.store.update_task_status("AT-407", schema.NEEDS_DECISION, source="test")
        self.gh_runner.set_pr(42, state="CLOSED")
        self.outcomes()
        self.assertEqual(self.status_of("AT-407"), schema.CANCELLED)
        self.assertTrue(worktree.is_dir())

    def test_a_pr_closed_while_queued_for_reintegration_cancels_and_dequeues(self) -> None:
        self.make_reviewing_task("AT-408")
        self.store.update_task_status("AT-408", schema.READY_FOR_INTEGRATION, source="test")
        enqueue_for_integration(self.integration, "AT-408", repo="owner/repo")
        self.gh_runner.set_pr(42, state="CLOSED")
        self.outcomes()
        self.assertEqual(self.status_of("AT-408"), schema.CANCELLED)
        self.assertEqual(queue_for_repo(self.integration, "owner/repo"), [])


class FreshnessScopeTests(WatcherTestCase):
    """Freshness pick-up is the §32.0 condition; only needs_review is re-queued.

    Both ticks now pick up every Ticket of their own repository with an open
    PR, whatever its status. Re-integration is still a lifecycle action, so
    the freshness tick re-queues only needs_review: §13 says a paused Ticket
    does not enter integration, and a needs_decision Ticket waits for a human.
    """

    def test_a_stale_paused_ticket_is_picked_up_but_not_requeued(self) -> None:
        self.make_reviewing_task("AT-420")
        self.store.update_task_status("AT-420", schema.PAUSED, source="test")
        self.fixture.advance_target()
        results = self.freshness()
        self.assertEqual([(r.task_key, r.stale, r.requeued) for r in results], [("AT-420", True, False)])
        self.assertEqual(self.status_of("AT-420"), schema.PAUSED)
        self.assertEqual(queue_for_repo(self.integration, "owner/repo"), [])



class CrossRepoScopeTests(unittest.TestCase):
    """§32.0 — every watcher tick handles only its own repository.

    Two repositories each have PR #42 open. Before this ruling, a tick for
    repo A also picked up repo B's Ticket, polled PR #42 through repo A's
    adapter, and wrote repo A's PR state onto repo B's Ticket. Each direction
    is checked: A's tick must not read, poll, transition or queue B's Ticket,
    and B's tick must not do so to A's.
    """

    REPOS = ("owner/repo-a", "owner/repo-b")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.fixtures = {repo: GitFixture(self.root / repo.split("/")[1]) for repo in self.REPOS}
        self.keys = {"owner/repo-a": "AT-501", "owner/repo-b": "AT-502"}
        self.worktrees: dict[str, Path] = {}
        self.gh = {repo: FakeGhRunner(repo=repo) for repo in self.REPOS}
        for repo in self.REPOS:
            self._make_reviewing_ticket(repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_reviewing_ticket(self, repo: str) -> None:
        fixture, key = self.fixtures[repo], self.keys[repo]
        worktree = fixture.create_task_worktree(key)
        fixture.commit_in(worktree, f"{key}.txt", "f\n", "feature")
        git_ops.push_branch(worktree, remote="origin", branch=f"task/{key}", base_branch="main")
        artifact_dir = self.root / "artifacts" / key
        artifact_dir.mkdir(parents=True)
        self.store.upsert_task(
            TaskRecord(task_key=key, project=repo.split("/")[1], status=schema.NEEDS_REVIEW,
                       repo_path=fixture.repo, artifact_dir=artifact_dir)
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(task_key=key, repo_path=fixture.repo, worktree_path=worktree,
                               branch=f"task/{key}", base_branch="main",
                               base_sha=fixture.target_sha(), status="active")
        )
        head = git_ops.head_sha(worktree)
        self.integration.update_pr_state(
            key, pr_number=42, pr_url=f"https://github.com/{repo}/pull/42", pr_state="open",
            pr_head_sha=head, integrated_base_sha=fixture.target_sha(),
        )
        self.gh[repo].set_pr(42, state="OPEN", headRefName=f"task/{key}", baseRefName="main",
                             headRefOid=head)
        self.worktrees[repo] = worktree

    def _request(self, repo: str) -> WatcherRequest:
        return WatcherRequest(repo=repo, repo_path=self.fixtures[repo].repo,
                              db_path=self.db_path, confirm_poll=True)

    def _snapshot(self, repo: str) -> tuple:
        key = self.keys[repo]
        return (
            asdict(self.store.get_task(key)),
            self.integration.get_pr_state(key),
            self.integration.get_integration_state(key),
            [(e.event_type, e.payload_json) for e in self.store.list_task_events(key)],
            self.integration.list_review_evidence(key),
        )

    def _other(self, repo: str) -> str:
        return next(r for r in self.REPOS if r != repo)

    def _assert_pr_tick_is_isolated(self, tick_repo: str) -> None:
        other = self._other(tick_repo)
        before = self._snapshot(other)
        # The tick's own PR #42 was closed unmerged. Had the tick picked up the
        # other repository's Ticket, it would have cancelled that one too.
        self.gh[tick_repo].set_pr(42, state="CLOSED")
        outcomes = poll_pr_outcomes(
            self._request(tick_repo), store=self.store, integration_store=self.integration,
            github=GitHubPrAdapter(tick_repo, runner=self.gh[tick_repo]),
        )
        self.assertEqual([o.task_key for o in outcomes], [self.keys[tick_repo]])
        # Exactly one gh call — for the tick's own Ticket. Both Tickets are PR
        # #42, so a call for the other Ticket would have been a second call.
        self.assertEqual(len(self.gh[tick_repo].calls), 1)
        self.assertEqual(self.gh[other].calls, [])
        self.assertEqual(self._snapshot(other), before)
        self.assertEqual(self.store.get_task(self.keys[tick_repo]).status, schema.CANCELLED)

    def _assert_freshness_tick_is_isolated(self, tick_repo: str) -> None:
        from v1_step2_fixtures import git as raw_git

        other = self._other(tick_repo)
        for fixture in self.fixtures.values():
            fixture.advance_target()
        other_origin_main = raw_git(self.worktrees[other], "rev-parse", "origin/main").strip()
        before = self._snapshot(other)
        outcomes = poll_target_freshness(
            self._request(tick_repo), store=self.store, integration_store=self.integration
        )
        self.assertEqual([(o.task_key, o.requeued) for o in outcomes], [(self.keys[tick_repo], True)])
        self.assertEqual([e.task_key for e in queue_for_repo(self.integration, tick_repo)],
                         [self.keys[tick_repo]])
        self.assertEqual(queue_for_repo(self.integration, other), [])
        self.assertEqual(self._snapshot(other), before)
        # Not even read through git: the other repository was never fetched.
        self.assertEqual(
            raw_git(self.worktrees[other], "rev-parse", "origin/main").strip(), other_origin_main
        )

    def test_repo_a_pr_tick_does_not_touch_repo_b_ticket(self) -> None:
        self._assert_pr_tick_is_isolated("owner/repo-a")

    def test_repo_b_pr_tick_does_not_touch_repo_a_ticket(self) -> None:
        self._assert_pr_tick_is_isolated("owner/repo-b")

    def test_repo_a_freshness_tick_does_not_touch_repo_b_ticket(self) -> None:
        self._assert_freshness_tick_is_isolated("owner/repo-a")

    def test_repo_b_freshness_tick_does_not_touch_repo_a_ticket(self) -> None:
        self._assert_freshness_tick_is_isolated("owner/repo-b")


if __name__ == "__main__":
    unittest.main()
