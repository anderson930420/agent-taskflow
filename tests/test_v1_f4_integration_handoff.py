"""V1 FOLLOWUPS F4 acceptance gate: SPEC §43.12 integration handoff.

Every test drives the real path: a Ticket is created through Step 1's creation
service and dispatched through the real Dispatcher, and the queue is then read
through Step 2's public API. Nothing here hand-enqueues.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import (  # noqa: E402
    RecordingExecutor,
    RecordingValidator,
    git,
    make_fixture,
)

from agent_taskflow import dispatcher as dispatcher_module  # noqa: E402
from agent_taskflow.integration_handoff import (  # noqa: E402
    ALREADY_QUEUED,
    COMPLETION_STATUS,
    ENQUEUED,
    HANDOFF_EVENT,
    HANDOFF_SOURCE,
    QUEUE_NAME,
    SKIPPED,
    handoff_completed_implementation,
)
from agent_taskflow.integration_queue import queue_for_repo  # noqa: E402
from agent_taskflow.integration_store import IntegrationStore  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_status_reset import (  # noqa: E402
    TaskStatusResetRequest,
    reset_task_status,
)
from agent_taskflow.ticket_creation import TicketCreationRequest, create_ticket  # noqa: E402
from agent_taskflow.ticket_repositories import TicketRepository  # noqa: E402
from agent_taskflow.ticket_store import TicketStore  # noqa: E402


ALPHA = "owner/alpha"
BETA = "owner/beta"


class HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        # The registry entry a Ticket is created from carries the GitHub repo
        # the per-repo integration queue is keyed on (§11, §22).
        self.fx.repository = replace(self.fx.repository, github_repo=ALPHA)
        self.store = TaskMirrorStore(self.fx.db_path)
        self.integration = IntegrationStore(self.fx.db_path)

    # -- helpers -----------------------------------------------------------
    def queue(self, repo: str) -> list:
        return queue_for_repo(self.integration, repo)

    def queued_keys(self, repo: str) -> list[str]:
        return [entry.task_key for entry in self.queue(repo)]

    def handoff_events(self, task_key: str) -> list[dict]:
        return [e for e in self.fx.events(task_key) if e["event_type"] == HANDOFF_EVENT]

    def second_repository(self) -> TicketRepository:
        repo = self.fx.root / "repo-beta"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "f4@example.invalid")
        git(repo, "config", "user.name", "F4")
        (repo / "README.md").write_text("beta\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")
        return TicketRepository(
            repository="f4-beta",
            repo_path=repo,
            worktrees_dir=repo / ".worktrees",
            artifacts_root=self.fx.artifacts,
            base_branch="main",
            branch_prefix="task/",
            github_repo=BETA,
        )

    def create_in(self, repository: TicketRepository, prompt: str):
        return create_ticket(
            TicketCreationRequest(
                repository=repository.repository, prompt=prompt, priority="normal"
            ),
            store=TicketStore(self.fx.db_path),
            repository=repository,
        ).ticket


class HandoffHappensTests(HandoffTestCase):
    """§43.12 — a finished implementation reaches its own repo's queue."""

    def test_a_completed_implementation_is_enqueued_once_in_its_own_repo_queue(self) -> None:
        ticket = self.fx.create_ticket("Hand the finished work to integration")
        key = ticket.task_key
        self.assertEqual(ticket.github_repo, ALPHA)
        self.assertEqual(self.queue(ALPHA), [])

        result = self.fx.dispatch(key)

        self.assertEqual(result.status, COMPLETION_STATUS)
        self.assertEqual(self.fx.status(key), COMPLETION_STATUS)
        entries = self.queue(ALPHA)
        self.assertEqual([e.task_key for e in entries], [key])
        self.assertEqual(entries[0].repo, ALPHA)
        self.assertEqual(entries[0].source, HANDOFF_SOURCE)
        self.assertEqual(entries[0].priority, ticket.priority)
        # The repo came from the Ticket, not from a default and not from the
        # registry slug.
        self.assertNotEqual(ALPHA, self.fx.repository.repository)
        self.assertEqual(self.queue(self.fx.repository.repository), [])

    def test_the_handoff_changes_no_lifecycle_status(self) -> None:
        ticket = self.fx.create_ticket("Handoff leaves the status alone")
        key = ticket.task_key
        self.fx.dispatch(key)

        self.assertEqual(self.fx.status(key), COMPLETION_STATUS)
        statuses = [e["payload"]["status"] for e in self.fx.status_events(key)]
        self.assertEqual(statuses[-1], COMPLETION_STATUS)
        self.assertNotIn("ready_for_integration", statuses)


class IdempotenceTests(HandoffTestCase):
    """Two dispatches, a retry and two ticks leave exactly one entry."""

    def test_reruns_reuse_the_original_queue_entry(self) -> None:
        ticket = self.fx.create_ticket("Idempotent handoff")
        key = ticket.task_key

        # A run that fails hands nothing off.
        self.assertEqual(self.fx.dispatch(key, RecordingExecutor("failed")).status, "failed")
        self.assertEqual(self.queue(ALPHA), [])

        # The operator retry, then the dispatch that actually completes.
        reset_task_status(
            TaskStatusResetRequest(
                task_key=key,
                db_path=self.fx.db_path,
                from_status="failed",
                reason="operator retry",
                actor="f4-test",
                confirm_reset=True,
            )
        )
        self.assertEqual(self.fx.dispatch(key).status, COMPLETION_STATUS)
        first = self.queue(ALPHA)[0]

        # A second dispatch (waiting_approval is a skipped status) and two more
        # calls, which is what a second and third scheduler tick would do.
        self.fx.dispatch(key)
        for _ in range(2):
            again = handoff_completed_implementation(
                self.store, key, task_status=COMPLETION_STATUS
            )
            self.assertEqual(again.status, ALREADY_QUEUED)
            self.assertEqual(again.enqueued_at, first.enqueued_at)

        entries = self.queue(ALPHA)
        self.assertEqual([e.task_key for e in entries], [key])
        self.assertEqual(entries[0].enqueued_at, first.enqueued_at)
        self.assertEqual(entries[0].sequence, first.sequence)
        self.assertEqual(len(self.handoff_events(key)), 1)


class OnlyOnSuccessTests(HandoffTestCase):
    """A Ticket that does not finish implementation is never enqueued."""

    def test_a_failed_run_is_never_enqueued(self) -> None:
        key = self.fx.create_ticket("Executor fails").task_key
        self.assertEqual(self.fx.dispatch(key, RecordingExecutor("failed")).status, "failed")
        self.assertEqual(self.queue(ALPHA), [])
        self.assertEqual(self.handoff_events(key), [])

    def test_a_red_validator_stops_at_needs_decision_without_enqueueing(self) -> None:
        key = self.fx.create_ticket("Validator is red").task_key
        result = self.fx.dispatch(key, RecordingExecutor(), (RecordingValidator(status="failed"),))
        self.assertEqual(result.status, "needs_decision")
        self.assertEqual(self.queue(ALPHA), [])
        self.assertEqual(self.handoff_events(key), [])

    def test_blocked_and_paused_tickets_are_never_enqueued(self) -> None:
        for status in ("blocked", "paused"):
            with self.subTest(status=status):
                key = self.fx.create_ticket(f"Refused while {status}").task_key
                self.fx.set_status(key, status)
                self.fx.dispatch(key)
                self.assertEqual(self.fx.status(key), status)
                self.assertEqual(self.queue(ALPHA), [])
                self.assertEqual(self.handoff_events(key), [])

    def test_the_handoff_refuses_every_non_completion_status(self) -> None:
        key = self.fx.create_ticket("Direct call guard").task_key
        for status in ("failed", "blocked", "needs_decision", "paused", "created"):
            with self.subTest(status=status):
                result = handoff_completed_implementation(self.store, key, task_status=status)
                self.assertEqual(result.status, SKIPPED)
                self.assertEqual(self.queue(ALPHA), [])

    def test_a_legacy_mirror_row_is_never_enqueued(self) -> None:
        self.fx.add_legacy_task("AT-LEGACY-F4")
        result = handoff_completed_implementation(
            self.store, "AT-LEGACY-F4", task_status=COMPLETION_STATUS
        )
        self.assertEqual(result.status, SKIPPED)
        self.assertEqual(self.queue(ALPHA), [])

    def test_a_ticket_with_no_github_repo_is_not_enqueued(self) -> None:
        local_only = replace(self.fx.repository, github_repo=None)
        key = self.create_in(local_only, "No GitHub repo to integrate against").task_key

        self.assertEqual(self.fx.dispatch(key).status, COMPLETION_STATUS)

        self.assertEqual(self.queue(ALPHA), [])
        self.assertEqual(self.queue(local_only.repository), [])
        self.assertEqual(self.handoff_events(key), [])


class PerRepoIsolationTests(HandoffTestCase):
    """Completing a Ticket in repo A puts nothing in repo B's queue."""

    def test_queues_stay_isolated_per_repo(self) -> None:
        beta = self.second_repository()
        alpha_key = self.fx.create_ticket("Alpha work").task_key
        beta_key = self.create_in(beta, "Beta work").task_key

        self.assertEqual(self.fx.dispatch(alpha_key).status, COMPLETION_STATUS)
        self.assertEqual(self.queued_keys(ALPHA), [alpha_key])
        self.assertEqual(self.queue(BETA), [])

        self.assertEqual(self.fx.dispatch(beta_key).status, COMPLETION_STATUS)
        self.assertEqual(self.queued_keys(BETA), [beta_key])
        self.assertEqual(self.queued_keys(ALPHA), [alpha_key])


class AuditTests(HandoffTestCase):
    """§44 — every lifecycle mutation is auditable."""

    def test_the_handoff_writes_one_audit_event_naming_ticket_repo_and_queue(self) -> None:
        key = self.fx.create_ticket("Audited handoff").task_key
        self.fx.dispatch(key)

        events = self.handoff_events(key)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["source"], HANDOFF_SOURCE)
        self.assertEqual(event["payload"]["task_key"], key)
        self.assertEqual(event["payload"]["repo"], ALPHA)
        self.assertEqual(event["payload"]["queue"], QUEUE_NAME)
        self.assertIn(key, event["message"])
        self.assertIn(ALPHA, event["message"])

    def test_a_queue_failure_is_audited_and_never_fails_a_finished_run(self) -> None:
        key = self.fx.create_ticket("Queue is unavailable").task_key

        def boom(*args, **kwargs):
            raise RuntimeError("integration queue unavailable")

        original = dispatcher_module.handoff_completed_implementation
        dispatcher_module.handoff_completed_implementation = boom
        self.addCleanup(
            setattr, dispatcher_module, "handoff_completed_implementation", original
        )

        result = self.fx.dispatch(key)

        self.assertEqual(result.status, COMPLETION_STATUS)
        self.assertEqual(self.fx.status(key), COMPLETION_STATUS)
        self.assertIn("integration_handoff_failed", self.fx.event_kinds(key))
        self.assertEqual(self.queue(ALPHA), [])


class ResultContractTests(HandoffTestCase):
    def test_the_result_reports_what_happened(self) -> None:
        ticket = self.fx.create_ticket("Result contract")
        key = ticket.task_key
        self.store.update_task_status(key, COMPLETION_STATUS, source="f4-test")

        first = handoff_completed_implementation(self.store, key, task_status=COMPLETION_STATUS)
        self.assertEqual(first.status, ENQUEUED)
        self.assertEqual(first.task_key, key)
        self.assertEqual(first.repo, ALPHA)
        self.assertIsNotNone(first.enqueued_at)

        second = handoff_completed_implementation(self.store, key, task_status=COMPLETION_STATUS)
        self.assertEqual(second.status, ALREADY_QUEUED)
        self.assertEqual(second.repo, ALPHA)
        self.assertEqual(second.enqueued_at, first.enqueued_at)


if __name__ == "__main__":
    unittest.main()
