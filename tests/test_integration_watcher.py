"""Tests for agent_taskflow.integration_watcher (spec §25, §25.0, §32, §33, §35)."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
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



class PrStateChangeEventTests(WatcherTestCase):
    """RULINGS 70 (F10-FU1): a PR-state event only when a §32.1 field changes;
    ``pr_last_polled_at`` is the per-poll heartbeat."""

    def polled_events(self, task_key: str) -> list[dict]:
        return [
            json.loads(e.payload_json)
            for e in self.store.list_task_events(task_key)
            if e.event_type == "pr_state_polled"
        ]

    def poll_at(self, heartbeat: str):
        with unittest.mock.patch(
            "agent_taskflow.integration_watcher.utc_now_iso", return_value=heartbeat
        ):
            return self.outcomes()

    def test_only_a_changed_field_writes_an_event_and_every_poll_beats(self) -> None:
        self.make_reviewing_task("AT-701")
        # First poll: GitHub reports fields integration did not record.
        self.poll_at("2099-01-01T00:00:00+00:00")
        [first] = self.polled_events("AT-701")
        self.assertEqual(
            first["changed_fields"],
            {"review_decision": {"old": None, "new": "none"},
             "ci_status": {"old": None, "new": "none"}},
        )
        # Unchanged polls: no event, but the heartbeat advances each time.
        for minute in ("05", "10"):
            beat = f"2099-01-01T00:{minute}:00+00:00"
            self.poll_at(beat)
            self.assertEqual(len(self.polled_events("AT-701")), 1)
            self.assertEqual(self.integration.get_pr_state("AT-701")["pr_last_polled_at"], beat)
        # One field changes: exactly one more event, naming only that field.
        self.gh_runner.set_pr(42, statusCheckRollup=[
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}])
        self.poll_at("2099-01-01T00:15:00+00:00")
        events = self.polled_events("AT-701")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["changed_fields"],
                         {"ci_status": {"old": "none", "new": "pending"}})
        self.assertEqual(self.status_of("AT-701"), schema.NEEDS_REVIEW)
        self.poll_at("2099-01-01T00:20:00+00:00")
        self.assertEqual(len(self.polled_events("AT-701")), 2)

    def test_an_unconfirmed_poll_writes_neither_event_nor_heartbeat(self) -> None:
        self.make_reviewing_task("AT-702")
        self.outcomes(confirm_poll=False)
        self.assertEqual(self.polled_events("AT-702"), [])
        self.assertIsNone(self.integration.get_pr_state("AT-702")["pr_last_polled_at"])


class PollFailureTests(WatcherTestCase):
    """Review round 4, Ruling 29e, as amended by RULINGS 70 (OR-9).

    A failed poll is never swallowed: it is counted and audited with the
    original exception. One failure does not change the Ticket's lifecycle;
    the 6th consecutive failure stops it for a decision (§27.2.1) wherever
    the transition table lets integration move it there. A success resets
    the count with one recovery event. Failure events are deduplicated by
    fingerprint, sha256(exception type + newline + message).
    """

    def fail_polls_with(self, exc: BaseException) -> None:
        def runner(args, **kwargs):
            raise exc

        self.github = GitHubPrAdapter("owner/repo", runner=runner)

    def events_of(self, task_key: str, event_type: str) -> list[dict]:
        return [
            json.loads(e.payload_json)
            for e in self.store.list_task_events(task_key)
            if e.event_type == event_type
        ]

    def failed_events(self, task_key: str) -> list[dict]:
        return self.events_of(task_key, "pr_poll_failed")

    def counter(self, task_key: str) -> tuple[int | None, str | None]:
        state = self.integration.get_integration_state(task_key)
        return state["pr_poll_consecutive_failures"], state["pr_poll_failure_fingerprint"]

    @staticmethod
    def fingerprint(exc_type: str, message: str) -> str:
        return hashlib.sha256(f"{exc_type}\n{message}".encode("utf-8")).hexdigest()

    def poll_n(self, times: int, **overrides) -> list:
        return [self.outcomes(**overrides)[0] for _ in range(times)]

    def test_a_gh_error_is_audited_and_does_not_change_the_lifecycle(self) -> None:
        self.make_reviewing_task("AT-801")
        self.gh_runner.pulls.pop(42)  # gh now answers "no such PR", rc=1
        [outcome] = self.outcomes()
        self.assertIsNone(outcome.proposed_transition)
        self.assertIsNone(outcome.applied_transition)
        self.assertEqual(outcome.consecutive_poll_failures, 1)
        self.assertIn("GitHubPrError", outcome.poll_error)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        [event] = self.failed_events("AT-801")
        self.assertEqual(event["exception_type"], "GitHubPrError")
        self.assertIn("no such PR", event["exception_message"])
        self.assertEqual(event["pr_number"], 42)
        self.assertEqual(event["consecutive_failures"], 1)
        self.assertIsNone(event["previous_fingerprint"])
        self.assertEqual(
            event["fingerprint"],
            self.fingerprint("GitHubPrError", event["exception_message"]))
        self.assertEqual(self.counter("AT-801"), (1, event["fingerprint"]))
        self.assertEqual(self.events_of("AT-801", "pr_poll_escalated"), [])

    def test_every_failure_kind_is_audited_once_and_escalates_at_six(self) -> None:
        from v1_step2_fixtures import FakeCompletedProcess, unknown_gh_json_field_error

        def unknown_field(args, **kwargs):
            # What real gh did to every poll before Ruling 29a.
            return FakeCompletedProcess(returncode=1, stderr=unknown_gh_json_field_error("merged"))

        for number, (kind, runner, expected_type, expected_text) in enumerate((
            ("unknown field", unknown_field, "GitHubPrError", 'Unknown JSON field: "merged"'),
            ("missing gh", FileNotFoundError(2, "No such file or directory", "gh"),
             "FileNotFoundError", "No such file or directory"),
            ("undecodable", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
             "UnicodeDecodeError", "invalid start byte"),
        )):
            with self.subTest(kind=kind):
                key = f"AT-81{number}"
                self.make_reviewing_task(key, pr_number=50 + number)
                if isinstance(runner, BaseException):
                    self.fail_polls_with(runner)
                else:
                    self.github = GitHubPrAdapter("owner/repo", runner=runner)
                outcomes = self.poll_n(5, task_keys=[key])
                self.assertEqual(self.status_of(key), schema.NEEDS_REVIEW)
                self.assertEqual([o.applied_transition for o in outcomes], [None] * 5)
                [event] = self.failed_events(key)
                self.assertEqual(event["exception_type"], expected_type)
                self.assertIn(expected_text, event["exception_message"])
                [sixth] = self.outcomes(task_keys=[key])
                self.assertEqual(sixth.applied_transition, schema.NEEDS_DECISION)
                self.assertEqual(self.status_of(key), schema.NEEDS_DECISION)
                self.assertEqual(len(self.failed_events(key)), 1)
                [escalated] = self.events_of(key, "pr_poll_escalated")
                self.assertEqual(escalated["exception_type"], expected_type)
                self.assertEqual(escalated["consecutive_failures"], 6)
                self.assertEqual(escalated["transition"], schema.NEEDS_DECISION)

    def test_five_failures_then_a_success_then_six_failures(self) -> None:
        """RULINGS 70: no change for 5; a success resets; the 6th escalates."""
        self.make_reviewing_task("AT-801")
        working = self.github
        self.fail_polls_with(RuntimeError("gh: HTTP 502"))
        expected_fp = self.fingerprint("RuntimeError", "gh: HTTP 502")

        for count, outcome in enumerate(self.poll_n(5), start=1):
            self.assertEqual(outcome.consecutive_poll_failures, count)
            self.assertIsNone(outcome.applied_transition)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        self.assertEqual(self.counter("AT-801"), (5, expected_fp))
        self.assertEqual(len(self.failed_events("AT-801")), 1)
        self.assertEqual(self.events_of("AT-801", "pr_poll_escalated"), [])

        # One success: the counter resets and exactly one recovery is logged.
        self.github = working
        [ok] = self.outcomes()
        self.assertIsNone(ok.poll_error)
        self.assertEqual(self.counter("AT-801"), (None, None))
        [recovered] = self.events_of("AT-801", "pr_poll_recovered")
        self.assertEqual(recovered["consecutive_failures"], 5)
        self.assertEqual(recovered["previous_fingerprint"], expected_fp)
        self.assertFalse(recovered["escalated"])
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        self.outcomes()  # a second success has nothing to recover from
        self.assertEqual(len(self.events_of("AT-801", "pr_poll_recovered")), 1)

        # Six more failures: the count restarts at 1 and the 6th escalates.
        self.fail_polls_with(RuntimeError("gh: HTTP 502"))
        outcomes = self.poll_n(6)
        self.assertEqual([o.consecutive_poll_failures for o in outcomes], [1, 2, 3, 4, 5, 6])
        self.assertEqual([o.applied_transition for o in outcomes],
                         [None] * 5 + [schema.NEEDS_DECISION])
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        # One failure event per run of failures (the reset cleared the fingerprint).
        self.assertEqual([e["consecutive_failures"] for e in self.failed_events("AT-801")], [1, 1])
        [escalated] = self.events_of("AT-801", "pr_poll_escalated")
        self.assertEqual((escalated["consecutive_failures"], escalated["transition"],
                          escalated["task_status"]),
                         (6, schema.NEEDS_DECISION, schema.NEEDS_REVIEW))
        status_changes = [e for e in self.store.list_task_events("AT-801")
                          if e.event_type == "status_changed"]
        self.assertEqual(len(status_changes), 1)

        # Escalation happens once: a 7th failure adds nothing.
        [seventh] = self.outcomes()
        self.assertEqual(seventh.consecutive_poll_failures, 7)
        self.assertIsNone(seventh.applied_transition)
        self.assertEqual(len(self.events_of("AT-801", "pr_poll_escalated")), 1)
        self.assertEqual(len(self.failed_events("AT-801")), 2)

        # Recovery after an escalation is logged, but the human keeps the decision.
        self.github = working
        self.outcomes()
        recoveries = self.events_of("AT-801", "pr_poll_recovered")
        self.assertEqual(len(recoveries), 2)
        self.assertEqual(recoveries[1]["consecutive_failures"], 7)
        self.assertTrue(recoveries[1]["escalated"])
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertEqual(self.counter("AT-801"), (None, None))

    def test_failure_events_are_deduplicated_by_fingerprint(self) -> None:
        self.make_reviewing_task("AT-801")
        a, b = RuntimeError("gh: HTTP 502"), TimeoutError("gh: HTTP 502")
        fp_a = self.fingerprint("RuntimeError", "gh: HTTP 502")
        fp_b = self.fingerprint("TimeoutError", "gh: HTTP 502")
        fp_c = self.fingerprint("RuntimeError", "gh: HTTP 503")
        self.assertEqual(len({fp_a, fp_b, fp_c}), 3)  # type and message both count
        for exc, polls in ((a, 2), (b, 1), (RuntimeError("gh: HTTP 503"), 1), (a, 1)):
            self.fail_polls_with(exc)
            self.poll_n(polls)
        events = self.failed_events("AT-801")
        self.assertEqual(
            [(e["fingerprint"], e["previous_fingerprint"], e["consecutive_failures"])
             for e in events],
            [(fp_a, None, 1), (fp_b, fp_a, 3), (fp_c, fp_b, 4), (fp_a, fp_c, 5)])
        # The count spans fingerprint changes: the next failure is the 6th.
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        self.outcomes()
        self.assertEqual(len(self.failed_events("AT-801")), 4)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertEqual(self.counter("AT-801"), (6, fp_a))

    def test_a_ticket_returned_to_review_while_failing_escalates_once_per_return(self) -> None:
        """Review N1: an escalation is not lost when a human returns the Ticket."""
        self.make_reviewing_task("AT-801")
        working = self.github
        self.fail_polls_with(RuntimeError("gh: HTTP 502"))
        self.poll_n(6)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.outcomes()  # 7th: already escalated, nothing new
        self.assertEqual(len(self.events_of("AT-801", "pr_poll_escalated")), 1)

        for round_number, count in ((2, 8), (3, 10)):
            # A human returns the Ticket to review; GitHub is still failing.
            self.store.update_task_status("AT-801", schema.NEEDS_REVIEW, source="test")
            events = len(self.store.list_task_events("AT-801"))
            [preview] = self.outcomes(confirm_poll=False)
            self.assertEqual(preview.proposed_transition, schema.NEEDS_DECISION)
            self.assertEqual(len(self.store.list_task_events("AT-801")), events)
            [again] = self.outcomes()
            self.assertEqual(again.consecutive_poll_failures, count)
            self.assertEqual(again.applied_transition, schema.NEEDS_DECISION)
            self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
            escalations = self.events_of("AT-801", "pr_poll_escalated")
            self.assertEqual(len(escalations), round_number)
            self.assertEqual(
                (escalations[-1]["reason"], escalations[-1]["consecutive_failures"],
                 escalations[-1]["task_status"], escalations[-1]["transition"]),
                ("escalatable_again_while_failing", count, schema.NEEDS_REVIEW,
                 schema.NEEDS_DECISION))
            # At most once per return: the next failure adds nothing.
            [after] = self.outcomes()
            self.assertIsNone(after.applied_transition)
            self.assertEqual(len(self.events_of("AT-801", "pr_poll_escalated")), round_number)
        self.assertEqual(self.events_of("AT-801", "pr_poll_escalated")[0]["reason"],
                         "threshold_reached")
        self.assertEqual(len(self.failed_events("AT-801")), 1)  # one fingerprint throughout
        self.github = working
        self.outcomes()
        [recovered] = self.events_of("AT-801", "pr_poll_recovered")
        self.assertEqual((recovered["consecutive_failures"], recovered["escalated"]), (11, True))

    def test_a_ticket_paused_at_the_threshold_escalates_after_it_resumes(self) -> None:
        """Review N1: reaching 6 while paused does not use up the escalation."""
        self.make_reviewing_task("AT-801")
        self.store.update_task_status("AT-801", schema.PAUSED, source="test")
        self.fail_polls_with(RuntimeError("simulated"))
        self.poll_n(7)
        [at_threshold] = self.events_of("AT-801", "pr_poll_escalated")
        self.assertEqual((at_threshold["consecutive_failures"], at_threshold["transition"],
                          at_threshold["reason"]), (6, None, "threshold_reached"))
        self.assertEqual(self.status_of("AT-801"), schema.PAUSED)
        self.store.update_task_status("AT-801", schema.NEEDS_REVIEW, source="test")
        [resumed] = self.outcomes()
        self.assertEqual((resumed.consecutive_poll_failures, resumed.applied_transition),
                         (8, schema.NEEDS_DECISION))
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.poll_n(2)
        escalations = self.events_of("AT-801", "pr_poll_escalated")
        self.assertEqual([(e["consecutive_failures"], e["transition"]) for e in escalations],
                         [(6, None), (8, schema.NEEDS_DECISION)])

    def test_a_new_pr_number_resets_the_failure_count(self) -> None:
        """Review N2: a count never carries over to the Ticket's next PR."""
        self.make_reviewing_task("AT-801")
        self.fail_polls_with(RuntimeError("simulated"))
        self.poll_n(4)
        counted = self.counter("AT-801")
        self.assertEqual(counted[0], 4)
        # Rewriting the same PR number, or other fields, keeps the count.
        self.integration.update_pr_state("AT-801", pr_number=42, pr_state="open")
        self.integration.update_pr_state("AT-801", ci_status="pending")
        self.assertEqual(self.counter("AT-801"), counted)
        events = len(self.store.list_task_events("AT-801"))
        # A different PR starts from zero.
        self.integration.update_pr_state(
            "AT-801", pr_number=77, pr_url="https://github.com/owner/repo/pull/77")
        self.assertEqual(self.counter("AT-801"), (None, None))
        self.assertEqual(len(self.store.list_task_events("AT-801")), events)
        outcomes = self.poll_n(5)
        self.assertEqual([o.pr_number for o in outcomes], [77] * 5)
        self.assertEqual([o.consecutive_poll_failures for o in outcomes], [1, 2, 3, 4, 5])
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        self.assertEqual(self.events_of("AT-801", "pr_poll_escalated"), [])
        # D3's completion transaction is a second pr_number writer: the same
        # number keeps the count, a different one resets it in that transaction.
        completed = dict(pr_url="https://github.com/owner/repo/pull/77", pr_state="open",
                         integrated_base_sha=self.fixture.target_sha())
        self.integration.record_integration_completed(
            "AT-801", pr_fields=dict(pr_number=77, **completed),
            increment_reintegration=False, state_fields={})
        self.assertEqual(self.counter("AT-801")[0], 5)
        self.integration.record_integration_completed(
            "AT-801", pr_fields=dict(completed, pr_number=78,
                                     pr_url="https://github.com/owner/repo/pull/78"),
            increment_reintegration=False,
            state_fields=dict(last_integration_status="integrated"))
        self.assertEqual(self.counter("AT-801"), (None, None))
        self.assertEqual(
            self.integration.get_integration_state("AT-801")["last_integration_status"],
            "integrated")
        # A new PR number on a Ticket with no integration state is a no-op.
        self.make_reviewing_task("AT-802", pr_number=43)
        self.integration.update_pr_state("AT-802", pr_number=44)
        self.assertEqual(self.counter("AT-802"), (None, None))

    def test_a_failed_poll_records_no_pr_fields(self) -> None:
        self.make_reviewing_task("AT-801")
        before = self.integration.get_pr_state("AT-801")
        self.fail_polls_with(RuntimeError("simulated"))
        self.poll_n(6)
        after = self.integration.get_pr_state("AT-801")
        self.assertEqual(after, before)

    def test_a_paused_ticket_stays_paused_but_the_failure_is_audited(self) -> None:
        self.make_reviewing_task("AT-801")
        self.store.update_task_status("AT-801", schema.PAUSED, source="test")
        self.fail_polls_with(RuntimeError("simulated"))
        outcomes = self.poll_n(6)
        self.assertEqual([o.applied_transition for o in outcomes], [None] * 6)
        self.assertEqual(self.status_of("AT-801"), schema.PAUSED)
        self.assertEqual(len(self.failed_events("AT-801")), 1)
        [escalated] = self.events_of("AT-801", "pr_poll_escalated")
        self.assertIsNone(escalated["transition"])

    def test_a_queued_ticket_keeps_its_queue_slot_but_the_failure_is_audited(self) -> None:
        self.make_reviewing_task("AT-801")
        self.store.update_task_status("AT-801", schema.READY_FOR_INTEGRATION, source="test")
        self.fail_polls_with(RuntimeError("simulated"))
        self.poll_n(6)
        self.assertEqual(self.status_of("AT-801"), schema.READY_FOR_INTEGRATION)
        self.assertEqual(len(self.failed_events("AT-801")), 1)
        [escalated] = self.events_of("AT-801", "pr_poll_escalated")
        self.assertIsNone(escalated["transition"])

    def test_an_unconfirmed_poll_reports_the_failure_but_writes_nothing(self) -> None:
        self.make_reviewing_task("AT-801")
        self.fail_polls_with(RuntimeError("simulated"))
        [outcome] = self.outcomes(confirm_poll=False)
        self.assertEqual(outcome.poll_error, "RuntimeError: simulated")
        self.assertIsNone(outcome.proposed_transition)
        self.assertEqual(outcome.consecutive_poll_failures, 1)
        self.assertEqual(self.counter("AT-801"), (None, None))
        # Review N3: the first preview writes no event of any kind.
        self.assertEqual(self.failed_events("AT-801"), [])
        self.assertEqual(
            [e.event_type for e in self.store.list_task_events("AT-801")
             if e.source == "integration_watcher"], [])
        # After 5 confirmed failures a preview proposes, but never applies, the escalation.
        self.poll_n(5)
        events = len(self.store.list_task_events("AT-801"))
        [preview] = self.outcomes(confirm_poll=False)
        self.assertEqual(preview.consecutive_poll_failures, 6)
        self.assertEqual(preview.proposed_transition, schema.NEEDS_DECISION)
        self.assertIsNone(preview.applied_transition)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_REVIEW)
        self.assertEqual(len(self.store.list_task_events("AT-801")), events)
        self.assertEqual(self.counter("AT-801")[0], 5)

    def test_one_failing_poll_does_not_stop_the_others(self) -> None:
        self.make_reviewing_task("AT-801", pr_number=42)
        self.make_reviewing_task("AT-802", pr_number=43)
        self.gh_runner.pulls.pop(42)
        self.poll_n(6)
        self.assertEqual(self.status_of("AT-801"), schema.NEEDS_DECISION)
        self.assertEqual(self.status_of("AT-802"), schema.NEEDS_REVIEW)
        self.assertEqual(self.failed_events("AT-802"), [])
        self.assertEqual(self.counter("AT-802"), (None, None))
        self.assertEqual(self.events_of("AT-802", "pr_poll_recovered"), [])


class PollFailureSchemaTests(unittest.TestCase):
    """OR-9: the counter columns are Step 2 startup columns, added idempotently."""

    def test_the_columns_are_added_to_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            store = TaskMirrorStore(db)
            store.init_db()
            columns = ("pr_poll_consecutive_failures", "pr_poll_failure_fingerprint")
            with closing(sqlite3.connect(db)) as conn:
                info = {row[1]: row[2] for row in conn.execute(
                    "PRAGMA table_info(task_integration_state)")}
                self.assertEqual({name: info[name] for name in columns},
                                 {columns[0]: "INTEGER", columns[1]: "TEXT"})
                # A database from before this migration, with a row in it.
                for name in columns:
                    conn.execute(f"ALTER TABLE task_integration_state DROP COLUMN {name}")
                conn.execute("DELETE FROM schema_migrations WHERE name = 'v1_step2_pr_poll_failures'")
                conn.commit()
            store.upsert_task(TaskRecord(task_key="AT-900", project="demo", status=schema.NEEDS_REVIEW,
                                         repo_path=Path(tmp)))
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("INSERT INTO task_integration_state (task_key, behind_count, updated_at) "
                             "VALUES ('AT-900', 3, 't')")
            store.init_db()
            store.init_db()  # idempotent
            state = IntegrationStore(db).get_integration_state("AT-900")
            self.assertEqual(state["behind_count"], 3)
            self.assertEqual((state[columns[0]], state[columns[1]]), (None, None))
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE name = 'v1_step2_pr_poll_failures'"
                ).fetchone()[0], 1)


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
