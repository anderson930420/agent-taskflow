"""Concurrent-write rehearsal (V1 Step 4, SPEC §19.2).

N runtime processes write state, Attempt, lease, ObservedStep and evidence at
the same time. Afterwards every expected row is present, ``PRAGMA
integrity_check`` is ``ok``, the lifecycle event log replays without an invalid
transition, and the contention the writers ran into is observable.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.concurrency_rehearsal import (
    RUNTIME_STEPS,
    add_rehearsal_task,
    create_rehearsal_fixture,
    integrity_errors,
    lifecycle_log_errors,
    run_worker_processes,
)
from agent_taskflow.store import connect

WRITERS = 4
HEARTBEATS = 5
EVIDENCE_ROWS = 4


class ConcurrentWriteRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fixture = create_rehearsal_fixture(Path(cls.tmp.name) / "fixture")
        cls.task_keys = [f"AT-WRITE-{index}" for index in range(WRITERS)]
        for task_key in cls.task_keys:
            add_rehearsal_task(cls.fixture, task_key)
        requests: list[dict] = [
            {
                "op": "hold-lock",
                "db_path": str(cls.fixture.db_path),
                "hold_seconds": 0.4,
            }
        ]
        requests.extend(
            {
                "op": "write-mix",
                "db_path": str(cls.fixture.db_path),
                "task_key": task_key,
                "owner_id": f"writer-{index}",
                "heartbeats": HEARTBEATS,
                "evidence_rows": EVIDENCE_ROWS,
                "artifact_root": str(cls.fixture.artifact_root),
            }
            for index, task_key in enumerate(cls.task_keys)
        )
        cls.results = run_worker_processes(requests)
        cls.writer_results = [item for item in cls.results if item["op"] == "write-mix"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(connect(self.fixture.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def test_every_writer_ran_in_its_own_process_and_finished(self) -> None:
        self.assertEqual(len(self.writer_results), WRITERS)
        self.assertEqual(len({item["pid"] for item in self.results}), WRITERS + 1)
        for item in self.writer_results:
            self.assertEqual(item["outcome"], "done", item)

    def test_no_lost_state_attempt_or_lease_write(self) -> None:
        for item in self.writer_results:
            task = self.query(
                "SELECT task_id, status, active_attempt_id FROM tasks WHERE task_key = ?",
                (item["task_key"],),
            )[0]
            self.assertEqual(task["status"], "waiting_approval")
            self.assertIsNone(task["active_attempt_id"])
            attempts = self.query(
                "SELECT attempt_id, status, is_active FROM attempts WHERE task_id = ?",
                (task["task_id"],),
            )
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["attempt_id"], item["attempt_id"])
            self.assertEqual(attempts[0]["status"], "waiting_approval")
            self.assertEqual(attempts[0]["is_active"], 0)
            leases = self.query(
                "SELECT is_active, release_reason FROM runtime_leases WHERE attempt_id = ?",
                (item["attempt_id"],),
            )
            self.assertEqual(len(leases), 1)
            self.assertEqual(leases[0]["is_active"], 0)
            self.assertEqual(leases[0]["release_reason"], "runtime_waiting_approval")

    def test_no_lost_lifecycle_event(self) -> None:
        for item in self.writer_results:
            reasons = [
                row["reason_code"]
                for row in self.query(
                    "SELECT reason_code FROM lifecycle_events "
                    "WHERE attempt_id = ? ORDER BY event_id",
                    (item["attempt_id"],),
                )
            ]
            self.assertEqual(
                reasons,
                ["runtime_pickup_claimed"]
                + ["runtime_lease_heartbeat"] * HEARTBEATS
                + ["runtime_implementing", "runtime_validating", "runtime_waiting_approval"],
            )

    def test_no_lost_observed_step_or_activity(self) -> None:
        for item in self.writer_results:
            steps = self.query(
                "SELECT step_name, status FROM attempt_observed_steps "
                "WHERE attempt_id = ? ORDER BY step_order",
                (item["attempt_id"],),
            )
            self.assertEqual([row["step_name"] for row in steps], list(RUNTIME_STEPS))
            self.assertTrue(all(row["status"] == "passed" for row in steps))
            progress = self.query(
                "SELECT current_phase FROM attempt_progress WHERE attempt_id = ?",
                (item["attempt_id"],),
            )
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[0]["current_phase"], RUNTIME_STEPS[-1])

    def test_no_lost_evidence(self) -> None:
        for item in self.writer_results:
            validations = self.query(
                "SELECT COUNT(*) FROM task_events WHERE task_key = ? "
                "AND event_type = 'note' AND payload_json LIKE '%validation_result%'",
                (item["task_key"],),
            )[0][0]
            artifacts = self.query(
                "SELECT COUNT(*) FROM task_artifacts WHERE task_key = ?",
                (item["task_key"],),
            )[0][0]
            self.assertEqual(validations, EVIDENCE_ROWS)
            self.assertEqual(artifacts, EVIDENCE_ROWS)

    def test_task_status_history_is_exactly_the_lifecycle(self) -> None:
        for item in self.writer_results:
            statuses = [
                row[0]
                for row in self.query(
                    "SELECT json_extract(payload_json, '$.status') FROM task_events "
                    "WHERE task_key = ? AND event_type = 'status_changed' ORDER BY id",
                    (item["task_key"],),
                )
            ]
            self.assertEqual(
                statuses,
                ["preparing", "implementing", "validating", "waiting_approval"],
            )

    def test_integrity_check_is_ok(self) -> None:
        self.assertEqual(integrity_errors(self.fixture.db_path), [])
        rows = self.query("PRAGMA integrity_check")
        self.assertEqual([row[0] for row in rows], ["ok"])

    def test_event_log_has_no_invalid_lifecycle_transition(self) -> None:
        self.assertEqual(lifecycle_log_errors(self.fixture.db_path), [])

    def test_contention_is_observable(self) -> None:
        holder = next(item for item in self.results if item["op"] == "hold-lock")
        self.assertEqual(holder["outcome"], "done")
        waits = sum(item["contention"]["busy_waits"] for item in self.writer_results)
        longest = max(
            item["contention"]["busy_wait_seconds_max"] for item in self.writer_results
        )
        acquisitions = sum(
            item["contention"]["lock_acquisitions"] for item in self.writer_results
        )
        # Every writer started while another process held the write lock.
        self.assertGreaterEqual(waits, WRITERS)
        self.assertGreaterEqual(longest, 0.1)
        self.assertGreater(acquisitions, WRITERS * HEARTBEATS)
        self.assertTrue(
            all(item["contention"]["busy_timeouts"] == 0 for item in self.writer_results)
        )


class LifecycleLogCheckerTests(unittest.TestCase):
    """The replay checker itself must catch a corrupted log."""

    def test_checker_flags_an_illegal_edge_and_a_broken_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = create_rehearsal_fixture(Path(tmp) / "fixture")
            add_rehearsal_task(fixture, "AT-BAD-LOG")
            outcome = run_worker_processes(
                [
                    {
                        "op": "write-mix",
                        "db_path": str(fixture.db_path),
                        "task_key": "AT-BAD-LOG",
                        "owner_id": "writer",
                        "heartbeats": 1,
                        "evidence_rows": 1,
                        "artifact_root": str(fixture.artifact_root),
                    }
                ]
            )[0]
            self.assertEqual(lifecycle_log_errors(fixture.db_path), [])
            with closing(connect(fixture.db_path)) as conn, conn:
                task_id = conn.execute(
                    "SELECT task_id FROM tasks WHERE task_key = 'AT-BAD-LOG'"
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO lifecycle_events(
                        task_id, attempt_id, from_status, to_status,
                        reason_code, actor, timestamp, metadata_json
                    ) VALUES (?, ?, 'waiting_approval', 'implementing',
                              'forged', 'test', '2999-01-01T00:00:00Z', '{}')
                    """,
                    (task_id, outcome["attempt_id"]),
                )
            errors = lifecycle_log_errors(fixture.db_path)
        self.assertTrue(any("waiting_approval -> implementing" in error for error in errors))
        self.assertTrue(any("ends at" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
