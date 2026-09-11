"""Atomic claim rehearsal (V1 Step 4, SPEC §19.1).

N concurrent claimers on one Ticket — threads and separate processes, through
the explicit ``RuntimeAdmissionStore.claim()`` API and through the dispatcher's
``preparing`` path — produce exactly one winner, one Attempt and one lease.
Every loser gets a typed refusal and writes nothing.
"""

from __future__ import annotations

from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.concurrency_rehearsal import (
    DISPATCHER_PATH_REFUSALS,
    EXPLICIT_CLAIM_REFUSALS,
    add_rehearsal_task,
    create_rehearsal_fixture,
    dispatcher_preparing_claim,
    explicit_claim,
    run_thread_race,
    run_worker_processes,
)
from agent_taskflow.store import connect

THREADS = 8
PROCESSES = 4


class AtomicClaimTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = create_rehearsal_fixture(Path(self.tmp.name) / "fixture")

    def assert_single_owner(self, task_key: str, outcomes: list[dict]) -> None:
        winners = [item for item in outcomes if item["outcome"] == "claimed"]
        self.assertEqual(len(winners), 1, outcomes)
        losers = [item for item in outcomes if item["outcome"] != "claimed"]
        self.assertEqual(len(losers), len(outcomes) - 1)
        with closing(connect(self.fixture.db_path)) as conn:
            task = conn.execute(
                "SELECT task_id, status, active_attempt_id FROM tasks WHERE task_key = ?",
                (task_key,),
            ).fetchone()
            attempts = conn.execute(
                "SELECT attempt_id, is_active FROM attempts WHERE task_id = ?",
                (task["task_id"],),
            ).fetchall()
            leases = conn.execute(
                "SELECT lease_id, attempt_id, owner_id, is_active FROM runtime_leases "
                "WHERE task_id = ?",
                (task["task_id"],),
            ).fetchall()
            claim_events = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_events "
                "WHERE task_id = ? AND to_status = 'preparing' AND from_status <> 'preparing'",
                (task["task_id"],),
            ).fetchone()[0]
        self.assertEqual(len(attempts), 1, "exactly one Attempt exists")
        self.assertEqual(len(leases), 1, "exactly one lease exists")
        self.assertEqual(attempts[0]["is_active"], 1)
        self.assertEqual(leases[0]["is_active"], 1)
        self.assertEqual(task["status"], "preparing")
        self.assertEqual(task["active_attempt_id"], attempts[0]["attempt_id"])
        self.assertEqual(leases[0]["attempt_id"], attempts[0]["attempt_id"])
        self.assertEqual(winners[0]["attempt_id"], attempts[0]["attempt_id"])
        self.assertEqual(claim_events, 1, "exactly one claim is audited")

    def assert_typed_refusals(self, outcomes: list[dict], allowed: tuple[str, ...]) -> None:
        for item in outcomes:
            if item["outcome"] == "claimed":
                continue
            self.assertEqual(item["outcome"], "refused", item)
            self.assertIn(item["error_type"], allowed, item)


class ExplicitClaimRaceTests(AtomicClaimTestCase):
    def test_threads_racing_the_claim_api_have_exactly_one_winner(self) -> None:
        add_rehearsal_task(self.fixture, "AT-RACE-T")
        outcomes = run_thread_race(
            [
                (lambda index=index: explicit_claim(
                    self.fixture.db_path, "AT-RACE-T", owner_id=f"thread-{index}"
                ))
                for index in range(THREADS)
            ]
        )
        self.assert_single_owner("AT-RACE-T", outcomes)
        self.assert_typed_refusals(outcomes, EXPLICIT_CLAIM_REFUSALS)

    def test_processes_racing_the_claim_api_have_exactly_one_winner(self) -> None:
        add_rehearsal_task(self.fixture, "AT-RACE-P")
        outcomes = run_worker_processes(
            [
                {
                    "op": "claim",
                    "db_path": str(self.fixture.db_path),
                    "task_key": "AT-RACE-P",
                    "owner_id": f"process-{index}",
                }
                for index in range(PROCESSES)
            ]
        )
        self.assertEqual(len({item["pid"] for item in outcomes}), PROCESSES)
        self.assert_single_owner("AT-RACE-P", outcomes)
        self.assert_typed_refusals(outcomes, EXPLICIT_CLAIM_REFUSALS)


class DispatcherPreparingRaceTests(AtomicClaimTestCase):
    def test_threads_racing_the_dispatcher_preparing_path_have_one_winner(self) -> None:
        add_rehearsal_task(self.fixture, "AT-DISPATCH-T")
        outcomes = run_thread_race(
            [
                (lambda index=index: dispatcher_preparing_claim(
                    self.fixture.db_path, "AT-DISPATCH-T", source=f"dispatcher-{index}"
                ))
                for index in range(THREADS)
            ]
        )
        self.assert_single_owner("AT-DISPATCH-T", outcomes)
        self.assert_typed_refusals(outcomes, DISPATCHER_PATH_REFUSALS)

    def test_processes_racing_the_dispatcher_preparing_path_have_one_winner(self) -> None:
        add_rehearsal_task(self.fixture, "AT-DISPATCH-P")
        outcomes = run_worker_processes(
            [
                {
                    "op": "dispatcher-claim",
                    "db_path": str(self.fixture.db_path),
                    "task_key": "AT-DISPATCH-P",
                    "owner_id": f"dispatcher-{index}",
                }
                for index in range(PROCESSES)
            ]
        )
        self.assertEqual(len({item["pid"] for item in outcomes}), PROCESSES)
        self.assert_single_owner("AT-DISPATCH-P", outcomes)
        self.assert_typed_refusals(outcomes, DISPATCHER_PATH_REFUSALS)

    def test_losers_write_no_attempt_resources(self) -> None:
        add_rehearsal_task(self.fixture, "AT-DISPATCH-RES")
        run_thread_race(
            [
                (lambda index=index: dispatcher_preparing_claim(
                    self.fixture.db_path, "AT-DISPATCH-RES", source=f"dispatcher-{index}"
                ))
                for index in range(THREADS)
            ]
        )
        with closing(connect(self.fixture.db_path)) as conn:
            resources = conn.execute(
                "SELECT COUNT(*) FROM attempt_resources WHERE task_key = ?",
                ("AT-DISPATCH-RES",),
            ).fetchone()[0]
        self.assertEqual(resources, 1)


class RefusalVocabularyTests(unittest.TestCase):
    def test_refusal_types_are_named(self) -> None:
        self.assertIn("RuntimeAdmissionError", EXPLICIT_CLAIM_REFUSALS)
        self.assertIn("RuntimeCapacityExceededError", EXPLICIT_CLAIM_REFUSALS)
        self.assertIn("ActiveAttemptExistsError", EXPLICIT_CLAIM_REFUSALS)
        self.assertNotIn("OperationalError", EXPLICIT_CLAIM_REFUSALS)
        self.assertNotIn("IntegrityError", EXPLICIT_CLAIM_REFUSALS)
        self.assertNotIn("OperationalError", DISPATCHER_PATH_REFUSALS)
        self.assertNotIn("IntegrityError", DISPATCHER_PATH_REFUSALS)


if __name__ == "__main__":
    unittest.main()
