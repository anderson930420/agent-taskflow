"""V1 Step 5: the explicit attempt_resources rebuild (ruling 26d)."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import REPO_ROOT, worker_env  # noqa: E402

from agent_taskflow.attempt_resources_schema import migrate_attempt_resources  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.ticket_worktree_schema import (  # noqa: E402
    TICKET_WORKTREE_MIGRATION_SCRIPT,
    TICKET_WORKTREE_RESOURCES_MIGRATION,
    TicketWorktreeMigrationError,
    TicketWorktreeMigrationRequired,
    migrate_ticket_worktree_resources,
    relaxed_table_sql,
    require_ticket_worktree_resources,
    ticket_worktree_resources_applied,
)


def schema_snapshot(db_path: Path) -> dict[str, dict[str, tuple[str, str | None]]]:
    """Every sqlite_master object by type -> name -> (tbl_name, sql)."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    snapshot: dict[str, dict[str, tuple[str, str | None]]] = {}
    for kind, name, table, sql in rows:
        snapshot.setdefault(kind, {})[name] = (table, sql)
    return snapshot


def unique_indexes(db_path: Path, table: str) -> list[tuple[str, ...]]:
    """Column tuples of every UNIQUE index on ``table`` (autoindexes included)."""
    with closing(sqlite3.connect(db_path)) as conn:
        result = []
        for row in conn.execute(f"PRAGMA index_list({table})"):
            if row[2]:
                columns = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})"))
                result.append(columns)
    return sorted(result)


def migration_names(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}


class RebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "state.db"
        TaskMirrorStore(self.db_path).init_db()
        migrate_attempt_resources(self.db_path)

    def test_schema_diff_is_exactly_the_two_unique_keywords(self) -> None:
        before = schema_snapshot(self.db_path)
        migrations_before = migration_names(self.db_path)
        unique_before = unique_indexes(self.db_path, "attempt_resources")

        result = migrate_ticket_worktree_resources(self.db_path)

        after = schema_snapshot(self.db_path)
        self.assertTrue(result.changed_schema)
        # Tables: only attempt_resources' definition changed, by exactly two keywords.
        self.assertEqual(set(before["table"]), set(after["table"]))
        for name, value in before["table"].items():
            if name != "attempt_resources":
                self.assertEqual(after["table"][name], value, name)
        old_sql = before["table"]["attempt_resources"][1]
        new_sql = after["table"]["attempt_resources"][1]
        self.assertEqual(
            new_sql,
            old_sql.replace("branch_name TEXT NOT NULL UNIQUE,", "branch_name TEXT NOT NULL,").replace(
                "worktree_path TEXT NOT NULL UNIQUE,", "worktree_path TEXT NOT NULL,"
            ),
        )
        self.assertNotEqual(new_sql, old_sql)
        self.assertIn("UNIQUE(task_id, attempt_number)", new_sql)
        # Named indexes and every trigger are byte-identical.
        named = lambda snap: {n: v for n, v in snap["index"].items() if not n.startswith("sqlite_autoindex")}
        self.assertEqual(named(after), named(before))
        self.assertEqual(after["trigger"], before["trigger"])
        self.assertEqual(before.get("view", {}), after.get("view", {}))
        # Unique constraints: the two relaxed columns are gone, the rest stay.
        unique_after = unique_indexes(self.db_path, "attempt_resources")
        self.assertEqual(
            sorted(set(unique_before) - set(unique_after)),
            [("branch_name",), ("worktree_path",)],
        )
        self.assertEqual(set(unique_after) - set(unique_before), set())
        for kept in (("artifact_root",), ("lock_path",), ("pid_path",), ("task_id", "attempt_number")):
            self.assertIn(kept, unique_after)
        # The only new migration record is this one.
        self.assertEqual(
            migration_names(self.db_path) - migrations_before,
            {TICKET_WORKTREE_RESOURCES_MIGRATION},
        )

    def test_rerun_is_a_schema_no_op(self) -> None:
        migrate_ticket_worktree_resources(self.db_path)
        snapshot = schema_snapshot(self.db_path)
        again = migrate_ticket_worktree_resources(self.db_path)
        self.assertFalse(again.changed_schema)
        self.assertFalse(again.migration_newly_recorded)
        self.assertEqual(schema_snapshot(self.db_path), snapshot)

    def test_lazy_attempt_resource_migration_does_not_restore_unique(self) -> None:
        migrate_ticket_worktree_resources(self.db_path)
        snapshot = schema_snapshot(self.db_path)
        migrate_attempt_resources(self.db_path)
        self.assertEqual(schema_snapshot(self.db_path), snapshot)

    def test_rebuild_preserves_existing_rows(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TRIGGER attempt_resources_identity_guard")
            conn.execute(
                """
                INSERT INTO attempt_resources(
                    attempt_id, task_id, task_key, attempt_number, owner_id, repo_path,
                    base_branch, base_sha, branch_name, worktree_root, worktree_path,
                    artifact_base_root, artifact_root, lock_path, pid_path, runtime_pid,
                    status, allocated_at, activated_at, released_at, reaped_at,
                    updated_at, release_reason)
                VALUES ('attempt-1', 'task:AT-1', 'AT-1', 1, 'owner', '/r', 'main', 'abc',
                        'attempt/at-1/1', '/r/.worktrees', '/r/.worktrees/at-1/attempt-1',
                        '/a', '/a/attempt-1', '/a/attempt-1/lock', '/a/attempt-1/pid', 1,
                        'released', 't0', 't1', 't2', NULL, 't2', 'task_status:blocked')
                """
            )
        migrate_attempt_resources(self.db_path)  # restores the dropped trigger
        with closing(sqlite3.connect(self.db_path)) as conn:
            before = conn.execute("SELECT * FROM attempt_resources").fetchall()
            # The seeded row has no parent task or Attempt on purpose.
            fk_before = conn.execute("PRAGMA foreign_key_check").fetchall()
        result = migrate_ticket_worktree_resources(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            after = conn.execute("SELECT * FROM attempt_resources").fetchall()
            fk_after = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(result.rows_copied, 1)
        self.assertEqual(after, before)
        self.assertEqual(fk_after, fk_before)

    def test_two_attempts_may_share_path_and_branch_after_the_rebuild_only(self) -> None:
        def insert(conn, attempt: str, number: int) -> None:
            conn.execute(
                """
                INSERT INTO attempt_resources(
                    attempt_id, task_id, task_key, attempt_number, owner_id, repo_path,
                    base_branch, branch_name, worktree_root, worktree_path,
                    artifact_base_root, artifact_root, lock_path, pid_path,
                    status, allocated_at, updated_at)
                VALUES (?, 'task:AT-1', 'AT-1', ?, 'owner', '/r', 'main', 'task/AT-1-x',
                        '/r/.worktrees', '/r/.worktrees/AT-1', '/a', ?, ?, ?,
                        'allocated', 't', 't')
                """,
                (attempt, number, f"/a/{attempt}", f"/a/{attempt}/lock", f"/a/{attempt}/pid"),
            )

        def shared_insert_succeeds() -> bool:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("DROP TRIGGER IF EXISTS attempt_resources_identity_guard")
                try:
                    insert(conn, "attempt-a", 1)
                    insert(conn, "attempt-b", 2)
                except sqlite3.IntegrityError:
                    return False
                finally:
                    conn.rollback()
            return True

        self.assertFalse(shared_insert_succeeds())
        migrate_ticket_worktree_resources(self.db_path)
        self.assertTrue(shared_insert_succeeds())
        # UNIQUE(task_id, attempt_number) still holds.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DROP TRIGGER IF EXISTS attempt_resources_identity_guard")
            insert(conn, "attempt-a", 1)
            with self.assertRaises(sqlite3.IntegrityError):
                insert(conn, "attempt-c", 1)
            conn.rollback()

    def test_unexpected_shape_is_refused(self) -> None:
        with self.assertRaises(TicketWorktreeMigrationError):
            relaxed_table_sql("CREATE TABLE attempt_resources (x TEXT)")


class FailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "state.db"
        TaskMirrorStore(self.db_path).init_db()

    def test_require_names_the_script_and_never_applies_it(self) -> None:
        before = schema_snapshot(self.db_path)
        self.assertFalse(ticket_worktree_resources_applied(self.db_path))
        with self.assertRaises(TicketWorktreeMigrationRequired) as ctx:
            require_ticket_worktree_resources(self.db_path)
        self.assertIn(TICKET_WORKTREE_MIGRATION_SCRIPT, str(ctx.exception))
        self.assertEqual(schema_snapshot(self.db_path), before)

    def test_missing_database_file_is_not_created(self) -> None:
        missing = self.db_path.parent / "absent.db"
        self.assertFalse(ticket_worktree_resources_applied(missing))
        self.assertFalse(missing.exists())

    def test_script_applies_it_once_and_reports_json(self) -> None:
        def run() -> dict:
            completed = subprocess.run(
                [sys.executable, TICKET_WORKTREE_MIGRATION_SCRIPT, "--db-path", str(self.db_path)],
                cwd=REPO_ROOT,
                env=worker_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

        first = run()
        second = run()
        self.assertTrue(first["ok"])
        self.assertTrue(first["rebuilt"])
        self.assertFalse(first["already_installed"])
        self.assertTrue(second["ok"])
        self.assertFalse(second["rebuilt"])
        self.assertTrue(second["already_installed"])
        self.assertEqual(first["migration"], TICKET_WORKTREE_RESOURCES_MIGRATION)
        require_ticket_worktree_resources(self.db_path)

    def test_script_requires_an_explicit_db_path(self) -> None:
        completed = subprocess.run(
            [sys.executable, TICKET_WORKTREE_MIGRATION_SCRIPT],
            cwd=REPO_ROOT,
            env=worker_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--db-path", completed.stderr)


if __name__ == "__main__":
    unittest.main()
