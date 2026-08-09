from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.lifecycle_control_schema import migrate_lifecycle_control
from agent_taskflow.project_class_control_schema import (
    PROJECT_CLASS_CONTROLS_MIGRATION,
    PROJECT_CLASS_CONTROL_SCOPES,
    migrate_project_class_controls,
)
from agent_taskflow.store import connect


class ProjectClassControlSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "state.db"

    @staticmethod
    def _rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
        return [
            tuple(row[column] for column in columns)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        ]

    def test_empty_database_migration_is_idempotent(self) -> None:
        migrate_project_class_controls(self.db_path)
        migrate_project_class_controls(self.db_path)

        with closing(connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (PROJECT_CLASS_CONTROLS_MIGRATION,),
            ).fetchone()[0]
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
            ).fetchone()[0].lower()
            guard = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'trigger'
                  AND name = 'level2_project_pause_admission_guard'
                """
            ).fetchone()

        self.assertEqual(count, 1)
        self.assertTrue(all(f"'{scope}'" in sql for scope in PROJECT_CLASS_CONTROL_SCOPES))
        self.assertIsNotNone(guard)

    def test_current_m1_schema_is_upgraded_without_parallel_identity_model(self) -> None:
        migrate_lifecycle_control(self.db_path)
        with closing(connect(self.db_path)) as conn:
            before = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
            ).fetchone()[0].lower()
        self.assertNotIn("'project'", before)
        self.assertNotIn("'task_class'", before)

        migrate_project_class_controls(self.db_path)

        with closing(connect(self.db_path)) as conn:
            after = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
            ).fetchone()[0].lower()
            task_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
        self.assertIn("'project'", after)
        self.assertIn("'task_class'", after)
        self.assertIn("project", task_columns)
        self.assertIn("task_class", task_columns)

    def test_existing_controls_events_and_immutability_are_preserved_row_for_row(self) -> None:
        migrate_lifecycle_control(self.db_path)
        with closing(connect(self.db_path)) as conn, conn:
            controls = (
                ("global", "*", "paused", "operator_pause_requested", "alice", "2026-01-01T00:00:00Z", 7, '{"a":1}'),
                ("task", "AT-1", "running", "operator_pause_cleared", "bob", "2026-01-02T00:00:00Z", 3, '{"b":2}'),
                ("attempt", "attempt-1", "kill_requested", "operator_kill_requested", "carol", "2026-01-03T00:00:00Z", 2, '{"c":3}'),
            )
            conn.executemany(
                """
                INSERT INTO runtime_controls(
                    scope_kind, scope_id, mode, reason_code, requested_by,
                    requested_at, generation, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                controls,
            )
            events = (
                ("global", "*", None, "paused", "operator_pause_requested", "alice", 7, "2026-01-01T00:00:00Z", '{"a":1}'),
                ("task", "AT-1", "paused", "running", "operator_pause_cleared", "bob", 3, "2026-01-02T00:00:00Z", '{"b":2}'),
                ("attempt", "attempt-1", None, "kill_requested", "operator_kill_requested", "carol", 2, "2026-01-03T00:00:00Z", '{"c":3}'),
            )
            conn.executemany(
                """
                INSERT INTO runtime_control_events(
                    scope_kind, scope_id, from_mode, to_mode, reason_code,
                    actor, generation, timestamp, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                events,
            )
            controls_before = self._rows(conn, "runtime_controls")
            events_before = self._rows(conn, "runtime_control_events")

        migrate_project_class_controls(self.db_path)

        with closing(connect(self.db_path)) as conn:
            self.assertEqual(self._rows(conn, "runtime_controls"), controls_before)
            self.assertEqual(self._rows(conn, "runtime_control_events"), events_before)
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            triggers = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            self.assertIn("ix_runtime_control_events_scope", indexes)
            self.assertIn("runtime_control_events_no_update", triggers)
            self.assertIn("runtime_control_events_no_delete", triggers)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                with conn:
                    conn.execute(
                        "UPDATE runtime_control_events SET actor = 'mallory' WHERE event_id = 1"
                    )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                with conn:
                    conn.execute("DELETE FROM runtime_control_events WHERE event_id = 1")

    def test_failed_rebuild_rolls_back_without_dropping_old_table(self) -> None:
        migrate_lifecycle_control(self.db_path)
        with closing(connect(self.db_path)) as conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                """
                INSERT INTO runtime_controls(
                    scope_kind, scope_id, mode, reason_code, requested_by,
                    requested_at, generation, metadata_json
                ) VALUES (
                    'invalid-scope', 'fixture', 'running',
                    'operator_pause_cleared', 'fixture-actor',
                    '2026-01-01T00:00:00Z', 1, '{}'
                )
                """
            )
            conn.commit()
            conn.execute("PRAGMA ignore_check_constraints = OFF")

        with self.assertRaises(sqlite3.IntegrityError):
            migrate_project_class_controls(self.db_path)

        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT scope_kind, scope_id FROM runtime_controls"
            ).fetchone()
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
            ).fetchone()[0].lower()
            marker = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (PROJECT_CLASS_CONTROLS_MIGRATION,),
            ).fetchone()
        self.assertEqual(tuple(row), ("invalid-scope", "fixture"))
        self.assertNotIn("'project'", sql)
        self.assertIsNone(marker)


if __name__ == "__main__":
    unittest.main()
