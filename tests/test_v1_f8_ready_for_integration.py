"""V1 FOLLOWUPS F8 acceptance gate: the Ticket success path ends
``ready_for_integration``.

The owner's ruling (RULINGS 53): a Ticket whose implementation and Taskflow
validators both pass transitions straight to ``ready_for_integration``. The
human gate stays where SPEC §31 and §44 put it — review of the GitHub PR before
merge. ``waiting_approval`` is the legacy GitHub-issue path's gate and keeps
working there (FOLLOWUPS F5 owns that split).

Every test drives the real path: a Ticket is created through Step 1's creation
service and dispatched through the real Dispatcher. The queue is read through
Step 2's public API. Nothing here hand-enqueues and nothing hand-writes the
terminal status.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import (  # noqa: E402
    RecordingExecutor,
    RecordingValidator,
    make_fixture,
)

from agent_taskflow.integration_handoff import (  # noqa: E402
    ALREADY_QUEUED,
    COMPLETION_STATUS,
    ENQUEUED,
    HANDOFF_EVENT,
    HANDOFF_SOURCE,
    SKIPPED,
    handoff_completed_implementation,
)
from agent_taskflow.integration_queue import queue_for_repo  # noqa: E402
from agent_taskflow.integration_store import IntegrationStore  # noqa: E402
from agent_taskflow.models import TaskWorktreeRecord  # noqa: E402
from agent_taskflow.runtime_admission import RuntimeAdmissionStore  # noqa: E402
from agent_taskflow.runtime_reaper import reap_stale_runtime  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_status_reset import (  # noqa: E402
    TaskStatusResetRequest,
    reset_task_status,
)
from agent_taskflow.ticket_lifecycle import (  # noqa: E402
    LEGACY_SUCCESS_STATUS,
    TICKET_SUCCESS_STATUS,
    ticket_success_status,
)


ALPHA = "owner/alpha"

# The §12 name the ruling puts at the end of the Ticket success path.
READY = "ready_for_integration"


class F8TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.repository = replace(self.fx.repository, github_repo=ALPHA)
        self.store = TaskMirrorStore(self.fx.db_path)
        self.integration = IntegrationStore(self.fx.db_path)

    # -- helpers -----------------------------------------------------------
    def queue(self, repo: str = ALPHA) -> list:
        return queue_for_repo(self.integration, repo)

    def queued_keys(self, repo: str = ALPHA) -> list[str]:
        return [entry.task_key for entry in self.queue(repo)]

    def handoff_events(self, task_key: str) -> list[dict]:
        return [e for e in self.fx.events(task_key) if e["event_type"] == HANDOFF_EVENT]

    def entered_ready_at(self, task_key: str) -> str:
        """The moment the Ticket entered `ready_for_integration` (§22.1)."""
        with closing(self.fx.connect()) as conn:
            rows = conn.execute(
                "SELECT payload_json, created_at FROM task_events"
                " WHERE task_key = ? AND event_type = 'status_changed' ORDER BY id",
                (task_key,),
            ).fetchall()
        stamps = [r["created_at"] for r in rows if READY in (r["payload_json"] or "")]
        self.assertEqual(len(stamps), 1, f"expected one entry into {READY}")
        return stamps[0]

    def approval_decisions(self, task_key: str) -> list[dict]:
        with closing(self.fx.connect()) as conn:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "approvals" not in tables:
                return []
            rows = conn.execute(
                "SELECT * FROM approvals WHERE task_key = ?", (task_key,)
            ).fetchall()
        return [dict(r) for r in rows]


class ChainClosesTests(F8TestCase):
    """§43.12 — a validated Ticket reaches the queue with no human step."""

    def test_a_validated_ticket_reaches_ready_for_integration_and_its_queue(self) -> None:
        ticket = self.fx.create_ticket("Close the §43.12 chain")
        key = ticket.task_key
        self.assertEqual(self.queue(), [])

        result = self.fx.dispatch(key)

        self.assertEqual(result.status, READY, result.summary)
        self.assertEqual(self.fx.status(key), READY)

        entries = self.queue()
        self.assertEqual([e.task_key for e in entries], [key])
        self.assertEqual(entries[0].repo, ALPHA)
        self.assertEqual(entries[0].source, HANDOFF_SOURCE)

    def test_no_human_step_sits_between_validating_and_ready_for_integration(self) -> None:
        key = self.fx.create_ticket("No approval in between").task_key
        self.fx.dispatch(key)

        statuses = [e["payload"]["status"] for e in self.fx.status_events(key)]
        self.assertEqual(statuses[-1], READY)
        # The transition is direct: validating is the status immediately before.
        self.assertEqual(statuses[statuses.index(READY) - 1], "validating")
        # The legacy gate is never entered, and no approval was recorded.
        self.assertNotIn(LEGACY_SUCCESS_STATUS, statuses)
        self.assertNotIn("accepted", statuses)
        self.assertEqual(self.approval_decisions(key), [])

    def test_the_terminal_status_releases_the_runtime_claim(self) -> None:
        # `ready_for_integration` must end the Attempt and release the lease
        # exactly as the old terminal status did; otherwise a finished Ticket
        # holds its capacity slot until the lease expires and the reaper then
        # destroys it (SPEC §19, §44).
        key = self.fx.create_ticket("Release the claim").task_key
        self.fx.dispatch(key)

        self.assertEqual(self.fx.status(key), READY)
        self.assertIsNone(self.fx.task_row(key)["active_attempt_id"])
        self.assertEqual(self.fx.leases(key, active_only=True), [])
        attempt = self.fx.attempts(key)[-1]
        self.assertEqual(attempt["is_active"], 0)
        self.assertEqual(attempt["execution_result"], "completed")
        self.assertEqual(attempt["validation_result"], "passed")

    def test_the_capacity_slot_is_free_for_the_next_ticket(self) -> None:
        first = self.fx.create_ticket("First").task_key
        self.assertEqual(self.fx.dispatch(first).status, READY)
        second = self.fx.create_ticket("Second").task_key
        self.assertEqual(self.fx.dispatch(second).status, READY)
        self.assertEqual(sorted(self.queued_keys()), sorted([first, second]))


class FifoKeyTests(F8TestCase):
    """§22.1 — the FIFO key is the moment the Ticket entered the status."""

    def test_the_queue_timestamp_is_when_the_ticket_entered_ready_for_integration(self) -> None:
        key = self.fx.create_ticket("FIFO key").task_key
        self.fx.dispatch(key)

        self.assertEqual(self.queue()[0].enqueued_at, self.entered_ready_at(key))

    def test_queue_order_follows_entry_order_not_priority(self) -> None:
        # Priority never affects integration order (§22.1).
        low = self.fx.create_ticket("Low first", priority="low").task_key
        critical = self.fx.create_ticket("Critical second", priority="critical").task_key

        self.fx.dispatch(low)
        self.fx.dispatch(critical)

        self.assertEqual(self.queued_keys(), [low, critical])
        self.assertEqual(self.queue()[0].enqueued_at, self.entered_ready_at(low))
        self.assertEqual(self.queue()[1].enqueued_at, self.entered_ready_at(critical))


class NoSelfApprovalTests(F8TestCase):
    """§44 — Taskflow still cannot approve, merge or push."""

    def test_nothing_approves_merges_or_pushes(self) -> None:
        ticket = self.fx.create_ticket("No self-approval")
        key = ticket.task_key
        head_before = self.fx.task_row(key)
        self.fx.dispatch(key)

        row = self.fx.task_row(key)
        self.assertEqual(self.approval_decisions(key), [])
        # Step 2 owns every PR field and none of them was written here (§32.1).
        for field in ("pr_number", "pr_url", "merge_commit_sha", "integrated_base_sha"):
            self.assertIsNone(row.get(field), field)
        self.assertFalse(row.get("pr_merged") or False)
        self.assertEqual(row["repo_path"], head_before["repo_path"])

    def test_integration_still_requires_its_confirmation_flag(self) -> None:
        # Fact 1 of the F8 brief: `confirm_integration` is operator-only, and
        # `integrate_task` has no automated caller at all. Reaching
        # `ready_for_integration` does not integrate anything by itself.
        from agent_taskflow.integration_controller import (
            IntegrationRequest,
            integrate_task,
        )

        key = self.fx.create_ticket("Queued, not integrated").task_key
        self.fx.dispatch(key)

        result = integrate_task(
            IntegrationRequest(
                task_key=key,
                repo=ALPHA,
                db_path=self.fx.db_path,
                dry_run=False,
                confirm_integration=False,
            )
        )
        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.confirmation_required)
        self.assertEqual(self.fx.status(key), READY)

    def test_the_queue_entry_alone_does_not_advance_the_ticket(self) -> None:
        key = self.fx.create_ticket("Still parked").task_key
        self.fx.dispatch(key)
        # No watcher, scheduler or cron moves it on; the row is where the
        # dispatcher left it.
        self.assertEqual(self.fx.status(key), READY)
        self.assertEqual(len(self.queue()), 1)


class FailuresUnchangedTests(F8TestCase):
    """§29 — failure vocabulary is untouched and never enqueues."""

    def test_a_red_validator_still_ends_needs_decision(self) -> None:
        key = self.fx.create_ticket("Validator red").task_key
        result = self.fx.dispatch(key, RecordingExecutor(), (RecordingValidator(status="failed"),))
        self.assertEqual(result.status, "needs_decision")
        self.assertEqual(self.fx.status(key), "needs_decision")
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.handoff_events(key), [])

    def test_an_executor_crash_still_ends_failed(self) -> None:
        key = self.fx.create_ticket("Executor crash").task_key
        result = self.fx.dispatch(key, RecordingExecutor("failed"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.handoff_events(key), [])

    def test_a_worktree_failure_still_ends_failed(self) -> None:
        ticket = self.fx.create_ticket("Worktree failure")
        key = ticket.task_key
        # The artifact directory cannot be created: governance refuses.
        ticket.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        ticket.artifact_dir.write_text("not a directory\n", encoding="utf-8")
        result = self.fx.dispatch(key)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.queue(), [])

    def test_lease_expiry_still_ends_failed(self) -> None:
        key = self.fx.create_ticket("Lease expiry").task_key
        claim = RuntimeAdmissionStore(self.fx.db_path).claim(
            key, owner_id="crashed-runner", ttl_seconds=60
        )
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z'"
                " WHERE lease_id = ?",
                (claim.lease_id,),
            )
        reap_stale_runtime(self.fx.db_path)
        self.assertEqual(self.fx.status(key), "failed")
        self.assertEqual(self.queue(), [])


class BlockedAndPausedTests(F8TestCase):
    """Refusal still writes nothing and never enqueues."""

    def test_blocked_and_paused_are_refused_untouched(self) -> None:
        for status in ("blocked", "paused"):
            with self.subTest(status=status):
                key = self.fx.create_ticket(f"Refused while {status}").task_key
                self.fx.set_status(key, status)
                before = self.fx.task_row(key)
                events = self.fx.events(key)

                self.fx.dispatch(key)

                self.assertEqual(self.fx.task_row(key), before)
                self.assertEqual(self.fx.events(key), events)
                self.assertEqual(self.queue(), [])


class LegacyUntouchedTests(F8TestCase):
    """FOLLOWUPS F5 owns the split; the legacy path keeps its own terminal."""

    def test_a_legacy_mirror_row_still_ends_waiting_approval(self) -> None:
        self.fx.add_legacy_task("AT-LEGACY-F8")
        TaskMirrorStore(self.fx.db_path).upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-LEGACY-F8",
                repo_path=self.fx.repo,
                worktree_path=self.fx.repo / ".worktrees" / "AT-LEGACY-F8",
                branch="task/AT-LEGACY-F8",
                base_branch="main",
                status="active",
            )
        )
        result = self.fx.dispatch("AT-LEGACY-F8", RecordingExecutor())

        self.assertEqual(result.status, LEGACY_SUCCESS_STATUS, result.summary)
        self.assertEqual(self.fx.status("AT-LEGACY-F8"), LEGACY_SUCCESS_STATUS)
        # A legacy row has no Ticket integration lifecycle, so it never queues.
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.handoff_events("AT-LEGACY-F8"), [])

    def test_the_success_status_helper_splits_ticket_from_legacy(self) -> None:
        self.assertEqual(ticket_success_status(ticket=True), TICKET_SUCCESS_STATUS)
        self.assertEqual(ticket_success_status(ticket=False), LEGACY_SUCCESS_STATUS)
        self.assertEqual(TICKET_SUCCESS_STATUS, READY)
        self.assertEqual(LEGACY_SUCCESS_STATUS, "waiting_approval")
        # The handoff accepts the Ticket terminal only.
        self.assertEqual(COMPLETION_STATUS, TICKET_SUCCESS_STATUS)


class IdempotenceTests(F8TestCase):
    """Reruns, retries and repeated handoffs leave exactly one entry."""

    def test_reruns_and_retries_keep_one_entry_with_its_original_timestamp(self) -> None:
        key = self.fx.create_ticket("Idempotent").task_key

        # A failed run hands nothing off.
        self.assertEqual(self.fx.dispatch(key, RecordingExecutor("failed")).status, "failed")
        self.assertEqual(self.queue(), [])

        reset_task_status(
            TaskStatusResetRequest(
                task_key=key,
                db_path=self.fx.db_path,
                from_status="failed",
                reason="operator retry",
                actor="f8-test",
                confirm_reset=True,
            )
        )
        self.assertEqual(self.fx.dispatch(key).status, READY)
        first = self.queue()[0]
        self.assertEqual(first.enqueued_at, self.entered_ready_at(key))

        # A second dispatch is refused, and two more handoff calls — what a
        # second and third scheduler tick would do — find the existing entry.
        self.fx.dispatch(key)
        for _ in range(2):
            again = handoff_completed_implementation(self.store, key, task_status=READY)
            self.assertEqual(again.status, ALREADY_QUEUED)
            self.assertEqual(again.enqueued_at, first.enqueued_at)

        entries = self.queue()
        self.assertEqual([e.task_key for e in entries], [key])
        self.assertEqual(entries[0].enqueued_at, first.enqueued_at)
        self.assertEqual(entries[0].sequence, first.sequence)
        self.assertEqual(len(self.handoff_events(key)), 1)
        self.assertEqual(self.fx.status(key), READY)

    def test_the_handoff_refuses_the_legacy_terminal_status(self) -> None:
        key = self.fx.create_ticket("Legacy status refused").task_key
        result = handoff_completed_implementation(
            self.store, key, task_status=LEGACY_SUCCESS_STATUS
        )
        self.assertEqual(result.status, SKIPPED)
        self.assertEqual(self.queue(), [])

    def test_a_direct_handoff_call_at_the_ticket_terminal_enqueues_once(self) -> None:
        key = self.fx.create_ticket("Direct call").task_key
        self.store.update_task_status(key, READY, source="f8-test")

        first = handoff_completed_implementation(self.store, key, task_status=READY)
        self.assertEqual(first.status, ENQUEUED)
        self.assertEqual(first.enqueued_at, self.entered_ready_at(key))

        second = handoff_completed_implementation(self.store, key, task_status=READY)
        self.assertEqual(second.status, ALREADY_QUEUED)
        self.assertEqual(second.enqueued_at, first.enqueued_at)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
