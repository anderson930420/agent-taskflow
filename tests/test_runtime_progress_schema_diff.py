"""Schema-diff gate for Step 3's migration (review item 3b).

The earlier guard was a source scan of five Step 3 files. It missed a
cross-module call: ``migrate_runtime_progress`` chained
``migrate_task_attempt_lifecycle``, which added six lifecycle columns to
``tasks`` and two lifecycle tables — none of it visible to a scan of Step 3's
own SQL.

These tests do not read source. They apply the migration to a real fixture
database and diff the **whole schema** before and after, so any column, table,
index or trigger that appears — from whatever module — is caught.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.attempt_schema import (
    TASK_ATTEMPT_LIFECYCLE_MIGRATION,
    migrate_task_attempt_lifecycle,
)
from agent_taskflow.runtime_progress_schema import (
    LIFECYCLE_MIGRATION_SCRIPT,
    RuntimeProgressPreconditionError,
    migrate_runtime_progress,
)
from agent_taskflow.store import init_db
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields


#: The only tables Step 3's migration may create.
STEP3_TABLES = frozenset({"attempt_progress", "attempt_observed_steps"})

#: Step 3 owns no column on ``tasks``. The allowed diff there is empty.
STEP3_TASK_COLUMNS: frozenset[str] = frozenset()


def schema_snapshot(db_path: Path) -> dict[str, dict]:
    """Every table's full column definitions, plus every index and trigger."""

    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        columns = {
            table: tuple(
                # (cid, name, type, notnull, dflt_value, pk)
                tuple(row)
                for row in conn.execute(f"PRAGMA table_info({table})")
            )
            for table in tables
        }
        objects = {
            kind: {
                name: (table, sql)
                for name, table, sql in conn.execute(
                    "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = ?",
                    (kind,),
                )
            }
            for kind in ("index", "trigger")
        }
    return {"columns": columns, "index": objects["index"], "trigger": objects["trigger"]}


class SchemaDiffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()


class MigrationSchemaDiffTests(SchemaDiffTestCase):
    """Apply Step 3's migration to a prepared fixture DB and diff everything."""

    def setUp(self) -> None:
        super().setUp()
        # Fixture: a database as an operator prepares it before Step 3's
        # migration — the base schema, Step 1's explicit Ticket-column
        # migration (it no longer runs at startup), and the lifecycle schema.
        # Step 3's migration runs on top of exactly this, so the diff also
        # proves it leaves Step 1's columns and unique indexes untouched.
        init_db(self.db_path)
        migrate_ticket_fields(self.db_path)
        migrate_task_attempt_lifecycle(self.db_path)
        self.before = schema_snapshot(self.db_path)
        migrate_runtime_progress(self.db_path)
        self.after = schema_snapshot(self.db_path)

    def test_only_step3_columns_are_new_on_tasks(self) -> None:
        before = {row[1] for row in self.before["columns"]["tasks"]}
        after = {row[1] for row in self.after["columns"]["tasks"]}
        self.assertEqual(after - before, set(STEP3_TASK_COLUMNS))
        self.assertEqual(before - after, set(), "no tasks column may disappear")

    def test_tasks_column_definitions_are_byte_identical(self) -> None:
        self.assertEqual(self.after["columns"]["tasks"], self.before["columns"]["tasks"])

    def test_every_preexisting_table_is_unchanged(self) -> None:
        for table, definition in self.before["columns"].items():
            with self.subTest(table=table):
                self.assertEqual(self.after["columns"].get(table), definition)

    def test_only_step3_tables_are_new(self) -> None:
        new_tables = set(self.after["columns"]) - set(self.before["columns"])
        self.assertEqual(new_tables, set(STEP3_TABLES))

    def test_new_indexes_and_triggers_belong_to_step3_tables_only(self) -> None:
        for kind in ("index", "trigger"):
            added = set(self.after[kind]) - set(self.before[kind])
            for name in sorted(added):
                with self.subTest(kind=kind, name=name):
                    table, _sql = self.after[kind][name]
                    self.assertIn(table, STEP3_TABLES)

    def test_no_preexisting_index_or_trigger_is_altered_or_removed(self) -> None:
        for kind in ("index", "trigger"):
            for name, definition in self.before[kind].items():
                with self.subTest(kind=kind, name=name):
                    self.assertEqual(self.after[kind].get(name), definition)

    def test_rerun_is_a_schema_no_op(self) -> None:
        migrate_runtime_progress(self.db_path)
        self.assertEqual(schema_snapshot(self.db_path), self.after)


class FailClosedPreconditionTests(SchemaDiffTestCase):
    """3a — without the lifecycle schema the migration refuses and writes nothing."""

    def test_base_schema_without_lifecycle_is_refused_with_no_schema_change(self) -> None:
        init_db(self.db_path)
        before = schema_snapshot(self.db_path)

        with self.assertRaises(RuntimeProgressPreconditionError) as ctx:
            migrate_runtime_progress(self.db_path)

        self.assertEqual(schema_snapshot(self.db_path), before)
        message = str(ctx.exception)
        self.assertIn(TASK_ATTEMPT_LIFECYCLE_MIGRATION, message)
        self.assertIn(LIFECYCLE_MIGRATION_SCRIPT, message)
        self.assertIn(str(self.db_path), message)

    def test_missing_database_is_refused_and_never_created(self) -> None:
        with self.assertRaises(RuntimeProgressPreconditionError) as ctx:
            migrate_runtime_progress(self.db_path)
        self.assertFalse(self.db_path.exists())
        self.assertIn(TASK_ATTEMPT_LIFECYCLE_MIGRATION, str(ctx.exception))

    def test_partial_lifecycle_schema_is_refused(self) -> None:
        # An `attempts` table alone is not enough: the join column on `tasks`
        # must be there too.
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY)")
        before = schema_snapshot(self.db_path)

        with self.assertRaises(RuntimeProgressPreconditionError) as ctx:
            migrate_runtime_progress(self.db_path)

        self.assertIn("tasks.task_id", str(ctx.exception))
        self.assertEqual(schema_snapshot(self.db_path), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
