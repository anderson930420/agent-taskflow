"""L2-M2.2: the producer Attempt of an integration run is exact or absent.

Every test drives the real path: a Ticket is dispatched through the real
Dispatcher, handed off through the real `handoff_completed_implementation`, and
read back through `resolve_producer_attempt_binding` and a real `integrate_task`
run. Nothing here writes a handoff event by hand, and no test asserts an Attempt
that the run did not itself produce.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import make_fixture  # noqa: E402
from v1_step2_fixtures import FakeGhRunner, GitFixture  # noqa: E402

from agent_taskflow import integration_schema as schema  # noqa: E402
from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle  # noqa: E402
from agent_taskflow.attempt_store import AttemptStore  # noqa: E402
from agent_taskflow.github_pr_adapter import GitHubPrAdapter  # noqa: E402
from agent_taskflow.integration_controller import (  # noqa: E402
    IntegrationRequest,
    integrate_task,
)
from agent_taskflow.integration_handoff import (  # noqa: E402
    ALREADY_QUEUED,
    BINDING_NONE,
    BINDING_PRODUCER_HANDOFF,
    COMPLETION_STATUS,
    ENQUEUED,
    HANDOFF_EVENT,
    REASON_BOUND,
    REASON_ENTRY_MISMATCH,
    REASON_NO_BINDING_IN_EVENT,
    REASON_NO_ENTRY,
    REASON_NO_HANDOFF,
    REASON_NO_PRODUCER,
    REASON_SUPERSEDED,
    REASON_TASK_MISMATCH,
    SUPERSEDED_EVENT,
    handoff_completed_implementation,
    resolve_producer_attempt_binding,
)
from agent_taskflow.integration_queue import (  # noqa: E402
    enqueue_for_integration,
    queue_for_repo,
    remove_from_queue,
)
from agent_taskflow.integration_store import IntegrationStore  # noqa: E402
from agent_taskflow.integration_validators import IntegrationValidatorSpec  # noqa: E402
from agent_taskflow.integration_watcher import (  # noqa: E402
    WatcherRequest,
    poll_target_freshness,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord  # noqa: E402
from agent_taskflow.runtime_progress_recorder import claimed_attempt_id  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.validation_summary import (  # noqa: E402
    ATTEMPT_BINDING_PRODUCER_HANDOFF,
    ATTEMPT_BINDING_RUNTIME_CLAIM,
    ATTEMPT_BINDING_UNBOUND,
)


ALPHA = "owner/alpha"
BETA = "owner/beta"
GREEN = (IntegrationValidatorSpec(name="unit", command=("true",)),)


class StubTicketStore:
    """Only the two Ticket fields the handoff reads (§22 repo, observability)."""

    def __init__(self, repo: str, priority: str = "normal") -> None:
        self.ticket = SimpleNamespace(github_repo=repo, priority=priority)

    def get_ticket(self, task_key: str):
        return self.ticket


class ProducerCaptureTests(unittest.TestCase):
    """The handoff records the Attempt that actually produced the work."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.repository = replace(self.fx.repository, github_repo=ALPHA)
        self.store = TaskMirrorStore(self.fx.db_path)
        self.integration = IntegrationStore(self.fx.db_path)

    # -- helpers -----------------------------------------------------------
    def queue(self, repo: str = ALPHA):
        return queue_for_repo(self.integration, repo)

    def binding(self, task_key: str, repo: str = ALPHA):
        return resolve_producer_attempt_binding(
            self.store, self.integration, task_key, repo=repo
        )

    def events(self, task_key: str, event_type: str) -> list[dict]:
        return [e for e in self.fx.events(task_key) if e["event_type"] == event_type]

    # -- tests -------------------------------------------------------------
    def test_the_dispatching_attempt_is_bound_to_its_own_queue_entry(self) -> None:
        key = self.fx.create_ticket("Bind the producer").task_key
        self.assertEqual(self.fx.dispatch(key).status, COMPLETION_STATUS)

        entry = self.queue()[0]
        produced_by = self.fx.attempts(key)[-1]["attempt_id"]
        payload = self.events(key, HANDOFF_EVENT)[0]["payload"]
        self.assertEqual(payload["producer_attempt_id"], produced_by)
        self.assertEqual(payload["queue_sequence"], entry.sequence)
        self.assertEqual(payload["producer_attempt_binding"], BINDING_PRODUCER_HANDOFF)

        binding = self.binding(key)
        self.assertTrue(binding.bound)
        self.assertEqual(binding.attempt_id, produced_by)
        self.assertEqual(binding.kind, BINDING_PRODUCER_HANDOFF)
        self.assertEqual(binding.reason_code, REASON_BOUND)
        self.assertEqual(binding.queue_sequence, entry.sequence)
        self.assertEqual(binding.enqueued_at, entry.enqueued_at)
        self.assertEqual(binding.task_identity_verified, "verified")

    def test_the_binding_is_the_attempt_the_executor_actually_ran_under(self) -> None:
        from step5_support import RecordingExecutor

        executor = RecordingExecutor()
        key = self.fx.create_ticket("Executor attempt").task_key
        self.fx.dispatch(key, executor)

        # The Attempt the executor was invoked with — not the newest Attempt row
        # and not the active pointer, which this status has already cleared.
        self.assertEqual(self.binding(key).attempt_id, executor.contexts[0].attempt_id)

    def test_the_producer_outlives_the_released_runtime_claim(self) -> None:
        key = self.fx.create_ticket("Claim released").task_key
        self.fx.dispatch(key)

        # The terminal status releases the claim, so nothing live remains to
        # ask. The handoff record is what makes the producer knowable.
        self.assertIsNone(self.fx.task_row(key)["active_attempt_id"])
        self.assertIsNone(claimed_attempt_id(self.store, key))
        self.assertTrue(self.binding(key).bound)

    def test_a_later_attempt_cannot_inherit_a_still_queued_entry(self) -> None:
        key = self.fx.create_ticket("Retry while queued").task_key
        self.fx.dispatch(key)
        first = self.queue()[0]
        produced_by = self.binding(key).attempt_id

        # A retry finishes the same Ticket while its entry is still queued. The
        # entry keeps its FIFO position, so the earlier Attempt would otherwise
        # be read as the producer of work it did not do.
        again = handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            producer_attempt_id="attempt-second-run",
        )
        self.assertEqual(again.status, ALREADY_QUEUED)
        self.assertTrue(again.producer_superseded)

        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.kind, BINDING_NONE)
        self.assertEqual(binding.reason_code, REASON_SUPERSEDED)
        self.assertEqual(binding.detail["recorded_producer_attempt_id"], produced_by)
        self.assertEqual(
            binding.detail["observed_producer_attempt_id"], "attempt-second-run"
        )

        # Idempotence and FIFO are untouched: one entry, its original position,
        # one handoff event, and one supersession however often the tick repeats.
        for _ in range(2):
            handoff_completed_implementation(
                self.store,
                key,
                task_status=COMPLETION_STATUS,
                producer_attempt_id="attempt-second-run",
            )
        entries = self.queue()
        self.assertEqual([e.task_key for e in entries], [key])
        self.assertEqual(entries[0].sequence, first.sequence)
        self.assertEqual(entries[0].enqueued_at, first.enqueued_at)
        self.assertEqual(len(self.events(key, HANDOFF_EVENT)), 1)
        self.assertEqual(len(self.events(key, SUPERSEDED_EVENT)), 1)

    def test_an_unbound_second_finisher_also_supersedes_the_first_producer(self) -> None:
        # Review finding M22-R1-N3: the later run holds no Attempt, so it cannot
        # name itself — but the queued entry may now carry its work, and Attempt
        # A must not stay bound to work it did not do.
        key = self.fx.create_ticket("Unbound second finisher").task_key
        self.fx.dispatch(key)
        first = self.queue()[0]
        produced_by = self.binding(key).attempt_id
        self.assertIsNotNone(produced_by)

        again = handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            producer_attempt_id=None,
        )
        self.assertEqual(again.status, ALREADY_QUEUED)
        self.assertTrue(again.producer_superseded)
        self.assertIsNone(again.producer_attempt_id)

        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertNotEqual(binding.attempt_id, produced_by)
        self.assertEqual(binding.kind, BINDING_NONE)
        self.assertEqual(binding.reason_code, REASON_SUPERSEDED)
        self.assertEqual(binding.detail["recorded_producer_attempt_id"], produced_by)
        self.assertIsNone(binding.detail["observed_producer_attempt_id"])
        self.assertEqual(binding.detail["observed_producer_binding"], BINDING_NONE)
        self.assertIn("held no Attempt", binding.reason)

        # Audited once however often the unbound finisher repeats, and the queue
        # keeps its FIFO position, entry id, timestamp and single handoff event.
        for _ in range(2):
            handoff_completed_implementation(
                self.store,
                key,
                task_status=COMPLETION_STATUS,
                producer_attempt_id=None,
            )
        entries = self.queue()
        self.assertEqual([entry.task_key for entry in entries], [key])
        self.assertEqual(entries[0].sequence, first.sequence)
        self.assertEqual(entries[0].enqueued_at, first.enqueued_at)
        self.assertEqual(len(self.events(key, HANDOFF_EVENT)), 1)
        self.assertEqual(len(self.events(key, SUPERSEDED_EVENT)), 1)
        superseded = self.events(key, SUPERSEDED_EVENT)[0]["payload"]
        self.assertEqual(superseded["queue_sequence"], first.sequence)
        self.assertEqual(superseded["recorded_producer_attempt_id"], produced_by)
        self.assertIsNone(superseded["observed_producer_attempt_id"])
        self.assertEqual(superseded["observed_producer_binding"], BINDING_NONE)

    def test_an_unbound_first_producer_is_not_superseded_by_a_bound_finisher(self) -> None:
        # The symmetric case is unchanged: there was never a producer to lose.
        key = self.fx.create_ticket("Unbound first").task_key
        self.store.update_task_status(key, COMPLETION_STATUS, source="test")
        handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            ticket_store=StubTicketStore(ALPHA),
            producer_attempt_id=None,
        )
        again = handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            ticket_store=StubTicketStore(ALPHA),
            producer_attempt_id="attempt-later-bound-run",
        )

        self.assertEqual(again.status, ALREADY_QUEUED)
        self.assertFalse(again.producer_superseded)
        self.assertEqual(self.events(key, SUPERSEDED_EVENT), [])
        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_NO_PRODUCER)

    def test_repeating_the_same_producer_supersedes_nothing(self) -> None:
        key = self.fx.create_ticket("Same producer twice").task_key
        self.fx.dispatch(key)
        produced_by = self.binding(key).attempt_id

        again = handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            producer_attempt_id=produced_by,
        )
        self.assertEqual(again.status, ALREADY_QUEUED)
        self.assertFalse(again.producer_superseded)
        self.assertEqual(self.events(key, SUPERSEDED_EVENT), [])
        self.assertEqual(self.binding(key).attempt_id, produced_by)

    def test_a_new_queue_generation_binds_only_its_own_producer(self) -> None:
        key = self.fx.create_ticket("Second generation").task_key
        self.fx.dispatch(key)
        first_entry = self.queue()[0]
        first_producer = self.binding(key).attempt_id

        # An integration run removes the entry when it publishes; a later run of
        # the same Ticket enqueues a new one.
        remove_from_queue(self.integration, key)
        self.assertEqual(self.queue(), [])
        second = handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            producer_attempt_id="attempt-generation-two",
        )
        self.assertEqual(second.status, ENQUEUED)

        entry = self.queue()[0]
        self.assertNotEqual(entry.sequence, first_entry.sequence)
        binding = self.binding(key)
        self.assertEqual(binding.attempt_id, "attempt-generation-two")
        self.assertNotEqual(binding.attempt_id, first_producer)
        self.assertEqual(binding.queue_sequence, entry.sequence)

    def test_a_stale_record_cannot_bind_an_entry_it_does_not_name(self) -> None:
        key = self.fx.create_ticket("Stale record").task_key
        self.fx.dispatch(key)

        # The entry this run created is gone; an operator re-queues the Ticket
        # by hand, so the newest entry was produced by nothing the record names.
        remove_from_queue(self.integration, key)
        enqueue_for_integration(self.integration, key, repo=ALPHA, source="operator")

        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_ENTRY_MISMATCH)
        self.assertIn("queue_sequence", binding.detail["mismatched"])

    def test_a_manual_entry_has_no_producer(self) -> None:
        key = self.fx.create_ticket("Manual entry").task_key
        self.store.update_task_status(key, COMPLETION_STATUS, source="operator")
        enqueue_for_integration(self.integration, key, repo=ALPHA, source="operator")

        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.kind, BINDING_NONE)
        self.assertEqual(binding.reason_code, REASON_NO_HANDOFF)
        self.assertIn("no execution handoff is recorded", binding.reason)

    def test_no_queue_entry_for_this_repository_is_not_a_producer(self) -> None:
        key = self.fx.create_ticket("Other repo").task_key
        self.fx.dispatch(key)

        binding = self.binding(key, repo=BETA)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_NO_ENTRY)

    def test_a_handoff_without_a_producer_stays_explicitly_unbound(self) -> None:
        # The legacy non-Ticket runner path holds no Attempt of its own.
        key = self.fx.create_ticket("No producer").task_key
        self.store.update_task_status(key, COMPLETION_STATUS, source="test")
        result = handoff_completed_implementation(
            self.store,
            key,
            task_status=COMPLETION_STATUS,
            ticket_store=StubTicketStore(ALPHA),
            producer_attempt_id=None,
        )
        self.assertEqual(result.status, ENQUEUED)

        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_NO_PRODUCER)
        self.assertIsNotNone(binding.queue_sequence)

    def test_a_handoff_recorded_before_producer_binding_is_not_bound(self) -> None:
        key = self.fx.create_ticket("Legacy record").task_key
        self.store.update_task_status(key, COMPLETION_STATUS, source="test")
        enqueue_for_integration(self.integration, key, repo=ALPHA, source="integration_handoff")
        # Exactly the payload the pre-M2.2 handoff wrote: no entry identity.
        self.store.record_task_event(
            key,
            HANDOFF_EVENT,
            "integration_handoff",
            message="legacy handoff",
            payload={"task_key": key, "repo": ALPHA, "queue": "integration_queue"},
        )

        binding = self.binding(key)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_NO_BINDING_IN_EVENT)

    def test_an_attempt_belonging_to_another_task_is_refused(self) -> None:
        first = self.fx.create_ticket("Owns the attempt").task_key
        second = self.fx.create_ticket("Borrows the attempt").task_key
        self.fx.dispatch(first)
        foreign = self.binding(first).attempt_id
        self.assertIsNotNone(foreign)

        self.store.update_task_status(second, COMPLETION_STATUS, source="test")
        handoff_completed_implementation(
            self.store,
            second,
            task_status=COMPLETION_STATUS,
            ticket_store=StubTicketStore(ALPHA),
            producer_attempt_id=foreign,
        )

        binding = self.binding(second)
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_TASK_MISMATCH)
        self.assertEqual(binding.task_identity_verified, "mismatch")
        self.assertEqual(binding.detail["rejected_attempt_id"], foreign)
        # The Ticket that really owns it keeps its own binding.
        self.assertEqual(self.binding(first).attempt_id, foreign)

    def test_two_tickets_keep_their_own_producers_in_fifo_order(self) -> None:
        first = self.fx.create_ticket("First").task_key
        second = self.fx.create_ticket("Second").task_key
        self.fx.dispatch(first)
        self.fx.dispatch(second)

        entries = self.queue()
        self.assertEqual([entry.task_key for entry in entries], [first, second])
        bindings = {key: self.binding(key).attempt_id for key in (first, second)}
        self.assertEqual(len(set(bindings.values())), 2)
        self.assertNotIn(None, bindings.values())
        for key, attempt_id in bindings.items():
            self.assertEqual(self.fx.attempts(key)[-1]["attempt_id"], attempt_id)


class ProducerReachesIntegrationTests(unittest.TestCase):
    """The bound producer travels into the integration run and its evidence."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        migrate_task_attempt_lifecycle(self.db_path)
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.attempts = AttemptStore(self.db_path)
        self.github = GitHubPrAdapter("owner/repo", runner=FakeGhRunner())
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()

    # -- helpers -----------------------------------------------------------
    def make_task(self, task_key: str) -> Path:
        worktree = self.fixture.create_task_worktree(task_key)
        artifact_dir = self.artifacts / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="demo",
                status=schema.READY_FOR_INTEGRATION,
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
        self.artifact_dir = artifact_dir
        return worktree

    def produce(self, task_key: str, *, attempt_id: str | None = None) -> str | None:
        """Create a real closed Attempt and hand it off, as a finished run does."""
        if attempt_id is None:
            self.attempts.register_task_identity(task_key, task_class="ticket")
            attempt = self.attempts.create_attempt(task_key, executor="fake")
            self.attempts.close_attempt(
                attempt.attempt_id,
                status="completed",
                reason_code="completed",
                actor="test-runner",
            )
            attempt_id = attempt.attempt_id
        handoff_completed_implementation(
            self.store,
            task_key,
            task_status=schema.READY_FOR_INTEGRATION,
            integration_store=self.integration,
            ticket_store=StubTicketStore("owner/repo"),
            producer_attempt_id=attempt_id,
        )
        return attempt_id

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
            owner="test-runtime",
        )
        kwargs.update(overrides)
        return integrate_task(
            IntegrationRequest(**kwargs),
            store=self.store,
            integration_store=self.integration,
            github=self.github,
        )

    def summary(self) -> dict:
        runs = sorted((self.artifact_dir / "validation-runs").iterdir())
        self.assertEqual(len(runs), 1)
        return json.loads((runs[0] / "validation-summary.json").read_text())

    def event_payloads(self, task_key: str, event_type: str) -> list[dict]:
        return [
            json.loads(event.payload_json or "{}")
            for event in self.store.list_task_events(task_key)
            if event.event_type == event_type
        ]

    # -- tests -------------------------------------------------------------
    def test_the_producer_reaches_the_integration_run_and_its_summary(self) -> None:
        self.make_task("AT-700")
        produced_by = self.produce("AT-700")

        result = self.integrate("AT-700")

        self.assertEqual(result.status, "integrated", result.summary)
        self.assertEqual(result.final_task_status, schema.NEEDS_REVIEW)
        binding = result.producer_attempt_binding
        self.assertEqual(binding["attempt_id"], produced_by)
        self.assertEqual(binding["kind"], BINDING_PRODUCER_HANDOFF)
        self.assertEqual(binding["reason_code"], REASON_BOUND)
        self.assertEqual(binding["task_identity_verified"], "verified")

        summary = self.summary()
        self.assertEqual(summary["attempt_id"], produced_by)
        self.assertEqual(summary["attempt_binding"], ATTEMPT_BINDING_PRODUCER_HANDOFF)
        self.assertNotEqual(summary["attempt_binding"], ATTEMPT_BINDING_RUNTIME_CLAIM)
        self.assertIn("not a live runtime claim", summary["attempt_binding_reason"])
        provenance = summary["attempt_binding_provenance"]
        self.assertEqual(provenance["queue_sequence"], binding["queue_sequence"])
        self.assertEqual(provenance["handoff_source"], "integration_handoff")
        self.assertIsNotNone(summary["integration_run_id"])
        self.assertEqual(summary["integration_run_id"], result.integration_run_id)

        started = self.event_payloads("AT-700", "integration_started")[0]
        completed = self.event_payloads("AT-700", "integration_completed")[0]
        self.assertEqual(started["producer_attempt_binding"]["attempt_id"], produced_by)
        self.assertEqual(completed["producer_attempt_binding"]["attempt_id"], produced_by)

    def test_a_run_the_pipeline_never_handed_off_stays_unbound(self) -> None:
        # A manual or watcher-driven run: nothing enqueued it.
        self.make_task("AT-701")

        result = self.integrate("AT-701")

        self.assertEqual(result.status, "integrated", result.summary)
        self.assertIsNone(result.producer_attempt_binding["attempt_id"])
        self.assertEqual(result.producer_attempt_binding["reason_code"], REASON_NO_ENTRY)
        summary = self.summary()
        self.assertIsNone(summary["attempt_id"])
        self.assertEqual(summary["attempt_binding"], ATTEMPT_BINDING_UNBOUND)
        # Explicitly null with its provenance, not silently absent.
        provenance = summary["attempt_binding_provenance"]
        self.assertIsNone(provenance["attempt_id"])
        self.assertEqual(provenance["kind"], BINDING_NONE)
        self.assertEqual(provenance["reason_code"], REASON_NO_ENTRY)

    def test_an_unresolvable_producer_does_not_change_the_gate(self) -> None:
        self.make_task("AT-702")
        self.produce("AT-702")
        handoff_completed_implementation(
            self.store,
            "AT-702",
            task_status=schema.READY_FOR_INTEGRATION,
            integration_store=self.integration,
            ticket_store=StubTicketStore("owner/repo"),
            producer_attempt_id="attempt-a-later-run",
        )

        result = self.integrate("AT-702")

        # Evidence loses the producer; lifecycle, validators and the PR do not.
        self.assertEqual(result.status, "integrated", result.summary)
        self.assertEqual(result.final_task_status, schema.NEEDS_REVIEW)
        self.assertTrue(result.validators_passed)
        self.assertEqual(
            result.producer_attempt_binding["reason_code"], REASON_SUPERSEDED
        )
        summary = self.summary()
        self.assertIsNone(summary["attempt_id"])
        self.assertEqual(summary["attempt_binding"], ATTEMPT_BINDING_UNBOUND)
        # The refused binding is still auditable on the run.
        started = self.event_payloads("AT-702", "integration_started")[0]
        self.assertEqual(
            started["producer_attempt_binding"]["reason_code"], REASON_SUPERSEDED
        )

    def test_a_red_validator_keeps_the_producer_on_the_stopped_run(self) -> None:
        self.make_task("AT-703")
        produced_by = self.produce("AT-703")

        result = self.integrate(
            "AT-703",
            validator_specs=(IntegrationValidatorSpec(name="unit", command=("false",)),),
        )

        self.assertEqual(result.status, "needs_decision")
        self.assertEqual(
            result.producer_attempt_binding["attempt_id"], produced_by
        )
        self.assertEqual(self.summary()["attempt_id"], produced_by)

    def test_a_watcher_requeue_carries_no_producer_of_its_own(self) -> None:
        self.make_task("AT-705")
        produced_by = self.produce("AT-705")
        self.assertEqual(self.integrate("AT-705").status, "integrated")
        self.assertEqual(
            resolve_producer_attempt_binding(
                self.store, self.integration, "AT-705", repo="owner/repo"
            ).reason_code,
            REASON_NO_ENTRY,
        )

        # The target advances and the real §32 watcher re-queues the Ticket for
        # re-integration. Its own queue event names no entry and no Attempt.
        self.fixture.advance_target()
        results = poll_target_freshness(
            WatcherRequest(
                repo="owner/repo",
                repo_path=self.fixture.repo,
                db_path=self.db_path,
                target_branch="main",
                remote="origin",
                confirm_poll=True,
            ),
            store=self.store,
            integration_store=self.integration,
        )
        self.assertEqual([(r.task_key, r.stale, r.requeued) for r in results], [("AT-705", True, True)])

        binding = resolve_producer_attempt_binding(
            self.store, self.integration, "AT-705", repo="owner/repo"
        )
        self.assertIsNone(binding.attempt_id)
        self.assertEqual(binding.reason_code, REASON_NO_BINDING_IN_EVENT)
        self.assertEqual(binding.handoff_source, "integration_watcher")
        self.assertNotIn(produced_by, binding.reason)

    def test_the_dry_run_default_is_unchanged(self) -> None:
        self.make_task("AT-704")
        self.produce("AT-704")

        result = self.integrate("AT-704", dry_run=True, confirm_integration=False)

        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.dry_run)
        self.assertTrue(result.confirmation_required)
        self.assertIsNone(result.producer_attempt_binding)
        self.assertFalse((self.artifact_dir / "validation-runs").exists())
        self.assertEqual(self.event_payloads("AT-704", "integration_started"), [])
        self.assertEqual(
            self.store.get_task("AT-704").status, schema.READY_FOR_INTEGRATION
        )


if __name__ == "__main__":  # pragma: no cover - direct unittest execution
    unittest.main()
