"""SQLite contention observability (V1 Step 4, SPEC §19.2).

``store.connect()`` hands out connections that count lock waits and BUSY
timeouts and log them as structured events, so contention between concurrent
runtimes is observable instead of silent.
"""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow import store as store_module
from agent_taskflow.sqlite_contention import (
    BUSY_WAIT_THRESHOLD_SECONDS,
    CONTENTION_LOGGER_NAME,
    ContentionObservingConnection,
    contention_snapshot,
    reset_contention_counters,
)
from agent_taskflow.store import connect


class ContentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "state.db"
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        reset_contention_counters()
        self.addCleanup(reset_contention_counters)

    def _hold_write_lock(self, seconds: float, holding: threading.Event) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO t(value) VALUES ('holder')")
            holding.set()
            time.sleep(seconds)


class ConnectFactoryTests(ContentionTestCase):
    def test_connect_returns_an_observing_connection(self) -> None:
        with closing(connect(self.db_path)) as conn:
            self.assertIsInstance(conn, ContentionObservingConnection)
            self.assertIsInstance(conn, sqlite3.Connection)

    def test_connect_keeps_its_existing_pragmas(self) -> None:
        with closing(connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("PRAGMA busy_timeout").fetchone()[0],
                store_module.SQLITE_BUSY_TIMEOUT_MS,
            )
            self.assertIs(conn.row_factory, sqlite3.Row)

    def test_rows_and_writes_behave_as_before(self) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO t(value) VALUES (?)", ("a",))
            conn.executemany("INSERT INTO t(value) VALUES (?)", [("b",), ("c",)])
        with closing(connect(self.db_path)) as conn:
            values = [row["value"] for row in conn.execute("SELECT value FROM t ORDER BY id")]
        self.assertEqual(values, ["a", "b", "c"])


class BusyWaitObservabilityTests(ContentionTestCase):
    def test_uncontended_lock_acquisition_is_counted_but_not_a_busy_wait(self) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
        snapshot = contention_snapshot()
        self.assertEqual(snapshot["lock_acquisitions"], 1)
        self.assertEqual(snapshot["busy_waits"], 0)
        self.assertEqual(snapshot["busy_timeouts"], 0)

    def test_waiting_for_another_writer_is_a_counted_busy_wait(self) -> None:
        holding = threading.Event()
        holder = threading.Thread(target=self._hold_write_lock, args=(0.3, holding))
        holder.start()
        self.assertTrue(holding.wait(5))
        with self.assertLogs(CONTENTION_LOGGER_NAME, level="INFO") as captured:
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO t(value) VALUES ('waiter')")
        holder.join()

        snapshot = contention_snapshot()
        self.assertGreaterEqual(snapshot["busy_waits"], 1)
        self.assertGreaterEqual(snapshot["busy_wait_seconds_max"], 0.1)
        self.assertGreater(snapshot["busy_wait_seconds_total"], 0)
        self.assertEqual(snapshot["busy_timeouts"], 0)
        events = [
            json.loads(record.getMessage().split(" ", 1)[1])
            for record in captured.records
            if record.getMessage().startswith("sqlite_busy_wait ")
        ]
        self.assertTrue(events)
        self.assertEqual(events[0]["event"], "sqlite_busy_wait")
        self.assertEqual(events[0]["db_path"], str(self.db_path))
        self.assertGreaterEqual(events[0]["waited_seconds"], BUSY_WAIT_THRESHOLD_SECONDS)
        self.assertEqual(events[0]["statement"], "BEGIN IMMEDIATE")

    def test_busy_timeout_is_counted_logged_and_still_raised(self) -> None:
        holding = threading.Event()
        holder = threading.Thread(target=self._hold_write_lock, args=(0.6, holding))
        holder.start()
        self.assertTrue(holding.wait(5))
        try:
            with mock.patch.object(store_module, "SQLITE_BUSY_TIMEOUT_MS", 50):
                with self.assertLogs(CONTENTION_LOGGER_NAME, level="WARNING") as captured:
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        with closing(connect(self.db_path)) as conn, conn:
                            conn.execute("BEGIN IMMEDIATE")
        finally:
            holder.join()

        self.assertIn("locked", str(raised.exception))
        snapshot = contention_snapshot()
        self.assertEqual(snapshot["busy_timeouts"], 1)
        timeout_events = [
            json.loads(record.getMessage().split(" ", 1)[1])
            for record in captured.records
            if record.getMessage().startswith("sqlite_busy_timeout ")
        ]
        self.assertEqual(len(timeout_events), 1)
        self.assertEqual(timeout_events[0]["event"], "sqlite_busy_timeout")
        self.assertEqual(timeout_events[0]["statement"], "BEGIN IMMEDIATE")

    def test_implicit_write_timeout_is_also_counted(self) -> None:
        holding = threading.Event()
        holder = threading.Thread(target=self._hold_write_lock, args=(0.6, holding))
        holder.start()
        self.assertTrue(holding.wait(5))
        try:
            with mock.patch.object(store_module, "SQLITE_BUSY_TIMEOUT_MS", 50):
                with self.assertLogs(CONTENTION_LOGGER_NAME, level="WARNING"):
                    with self.assertRaises(sqlite3.OperationalError):
                        with closing(connect(self.db_path)) as conn, conn:
                            conn.execute("INSERT INTO t(value) VALUES ('deferred')")
        finally:
            holder.join()
        self.assertEqual(contention_snapshot()["busy_timeouts"], 1)

    def test_counters_reset(self) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
        self.assertEqual(contention_snapshot()["lock_acquisitions"], 1)
        reset_contention_counters()
        self.assertEqual(
            contention_snapshot(),
            {
                "lock_acquisitions": 0,
                "busy_waits": 0,
                "busy_wait_seconds_total": 0.0,
                "busy_wait_seconds_max": 0.0,
                "busy_timeouts": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
