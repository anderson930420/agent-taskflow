"""V1 Step 1 Ticket columns: explicit migration, fail-closed startup.

PR #195 ruling 4a. Step 1's `tasks` columns and unique indexes are installed
only by the operator-run `scripts/migrate_ticket_fields.py`. Neither
`store.init_db()` nor `TicketStore.init_db()` applies them, and the Mission
Control API refuses to start — naming the script — until they exist.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow import store as store_module
from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, init_db as init_task_db
from agent_taskflow.ticket_fields_schema import (
    TASK_TICKET_COLUMNS,
    TASK_TICKET_INDEXES,
    TICKET_FIELDS_MIGRATION,
    TICKET_FIELDS_MIGRATION_SCRIPT,
    TicketFieldsMigrationRequired,
    TicketFieldsPreconditionError,
    migrate_ticket_fields,
    missing_ticket_fields,
    require_ticket_fields,
)
from agent_taskflow.ticket_store import TicketStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / TICKET_FIELDS_MIGRATION_SCRIPT
STEP1_COLUMNS = tuple(name for name, _sql in TASK_TICKET_COLUMNS)
STEP1_INDEXES = frozenset(name for name, _sql in TASK_TICKET_INDEXES)
ALL_MISSING = tuple(f"tasks.{name}" for name in STEP1_COLUMNS) + tuple(
    f"index {name}" for name, _sql in TASK_TICKET_INDEXES
)


def run_script(db_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", str(db_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def schema_objects(db_path: Path) -> set[tuple[str, str, str]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            (row[0], row[1], row[2])
            for row in conn.execute(
                "SELECT type, name, tbl_name FROM sqlite_master"
                " WHERE name NOT LIKE 'sqlite_%'"
            )
        }


def table_columns(db_path: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(db_path)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }


def migration_names(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}


class TicketFieldsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def start_app(self) -> None:
        with TestClient(create_app(self.db_path)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/api/tasks").status_code, 200)


class StartupGateTests(TicketFieldsTestCase):
    """Startup fails closed until the explicit migration has run."""

    def test_fresh_db_startup_refuses_and_names_the_script(self) -> None:
        with self.assertRaises(TicketFieldsMigrationRequired) as ctx:
            self.start_app()
        message = str(ctx.exception)
        self.assertIn(TICKET_FIELDS_MIGRATION_SCRIPT, message)
        self.assertIn(str(self.db_path), message)
        self.assertIn("tasks.prompt", message)

    def test_refused_startup_does_not_install_step1_columns(self) -> None:
        with self.assertRaises(TicketFieldsMigrationRequired):
            self.start_app()
        self.assertEqual(missing_ticket_fields(self.db_path), ALL_MISSING)

    def test_after_the_script_runs_startup_succeeds(self) -> None:
        with self.assertRaises(TicketFieldsMigrationRequired):
            self.start_app()

        completed = run_script(self.db_path)
        self.assertEqual(completed.returncode, 0, completed.stderr)

        self.start_app()
        self.assertEqual(missing_ticket_fields(self.db_path), ())

    def test_partial_install_still_refuses_and_names_what_is_missing(self) -> None:
        init_task_db(self.db_path)
        migrate_ticket_fields(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DROP INDEX ux_tasks_repo_branch")

        with self.assertRaises(TicketFieldsMigrationRequired) as ctx:
            self.start_app()
        self.assertIn("index ux_tasks_repo_branch", str(ctx.exception))
        self.assertIn(TICKET_FIELDS_MIGRATION_SCRIPT, str(ctx.exception))


class NoStartupApplyTests(TicketFieldsTestCase):
    """Neither init_db() applies Step 1's migration any more."""

    def test_task_store_init_db_does_not_apply_step1(self) -> None:
        init_task_db(self.db_path)
        self.assertEqual(missing_ticket_fields(self.db_path), ALL_MISSING)
        self.assertNotIn(TICKET_FIELDS_MIGRATION, store_module.SCHEMA_MIGRATIONS)
        self.assertNotIn(TICKET_FIELDS_MIGRATION, migration_names(self.db_path))

    def test_ticket_store_init_db_refuses_and_does_not_apply(self) -> None:
        with self.assertRaises(TicketFieldsMigrationRequired) as ctx:
            TicketStore(self.db_path).init_db()
        self.assertIn(TICKET_FIELDS_MIGRATION_SCRIPT, str(ctx.exception))
        self.assertEqual(missing_ticket_fields(self.db_path), ALL_MISSING)

    def test_legacy_migrations_stay_in_the_startup_path(self) -> None:
        init_task_db(self.db_path)
        self.assertEqual(
            migration_names(self.db_path),
            set(store_module.SCHEMA_MIGRATIONS),
        )


class MigrationTests(TicketFieldsTestCase):
    def test_migration_installs_everything_and_require_passes(self) -> None:
        init_task_db(self.db_path)
        result = migrate_ticket_fields(self.db_path)
        self.assertEqual(result.columns_added, STEP1_COLUMNS)
        self.assertEqual(set(result.indexes_added), STEP1_INDEXES)
        self.assertTrue(result.migration_newly_recorded)
        require_ticket_fields(self.db_path)

    def test_migration_is_idempotent(self) -> None:
        init_task_db(self.db_path)
        migrate_ticket_fields(self.db_path)
        objects = schema_objects(self.db_path)
        columns = table_columns(self.db_path)

        again = migrate_ticket_fields(self.db_path)

        self.assertEqual(again.columns_added, ())
        self.assertEqual(again.indexes_added, ())
        self.assertFalse(again.migration_newly_recorded)
        self.assertFalse(again.changed_schema)
        self.assertEqual(schema_objects(self.db_path), objects)
        self.assertEqual(table_columns(self.db_path), columns)

    def test_missing_database_is_refused_without_creating_it(self) -> None:
        with self.assertRaises(TicketFieldsPreconditionError) as ctx:
            migrate_ticket_fields(self.db_path)
        self.assertIn("agent_taskflow.store.init_db", str(ctx.exception))
        self.assertFalse(self.db_path.exists())

    def test_database_without_legacy_schema_is_refused_before_any_write(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        before = schema_objects(self.db_path)

        with self.assertRaises(TicketFieldsPreconditionError) as ctx:
            migrate_ticket_fields(self.db_path)
        self.assertIn("table tasks", str(ctx.exception))
        self.assertEqual(schema_objects(self.db_path), before)


class SchemaDiffTests(TicketFieldsTestCase):
    """The migration adds exactly Step 1's columns and indexes, nothing else."""

    def test_migration_adds_exactly_step1_columns_and_indexes(self) -> None:
        init_task_db(self.db_path)
        TaskMirrorStore(self.db_path).upsert_task(
            TaskRecord(
                task_key="AT-0009",
                project="forms",
                status="queued",
                repo_path=self.root / "forms",
                title="Legacy row",
            )
        )
        objects_before = schema_objects(self.db_path)
        columns_before = table_columns(self.db_path)
        migrations_before = migration_names(self.db_path)
        legacy_before = TaskMirrorStore(self.db_path).get_task("AT-0009")

        migrate_ticket_fields(self.db_path)

        objects_after = schema_objects(self.db_path)
        columns_after = table_columns(self.db_path)

        # sqlite_master: exactly the two indexes on `tasks` were added; no
        # table, trigger or index was added elsewhere or removed.
        self.assertEqual(
            objects_after - objects_before,
            {("index", name, "tasks") for name in STEP1_INDEXES},
        )
        self.assertEqual(objects_before - objects_after, set())

        # Columns: every table but `tasks` is byte-for-byte unchanged.
        self.assertEqual(set(columns_after), set(columns_before))
        for table, columns in columns_before.items():
            if table != "tasks":
                self.assertEqual(columns_after[table], columns, table)

        # `tasks`: existing columns unchanged, in place; exactly Step 1's
        # columns appended, all nullable TEXT with no default.
        tasks_before = columns_before["tasks"]
        tasks_after = columns_after["tasks"]
        self.assertEqual(tasks_after[: len(tasks_before)], tasks_before)
        appended = tasks_after[len(tasks_before):]
        self.assertEqual(tuple(column[1] for column in appended), STEP1_COLUMNS)
        for _cid, name, sql_type, notnull, default, pk in appended:
            self.assertEqual((sql_type, notnull, default, pk), ("TEXT", 0, None, 0), name)

        # Data: only the migration's own bookkeeping row; legacy rows intact.
        self.assertEqual(
            migration_names(self.db_path) - migrations_before,
            {TICKET_FIELDS_MIGRATION},
        )
        self.assertEqual(TaskMirrorStore(self.db_path).get_task("AT-0009"), legacy_before)
        with closing(sqlite3.connect(self.db_path)) as conn:
            values = conn.execute(
                f"SELECT {', '.join(STEP1_COLUMNS)} FROM tasks WHERE task_key = 'AT-0009'"
            ).fetchone()
        self.assertEqual(set(values), {None})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
