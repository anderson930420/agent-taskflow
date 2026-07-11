from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.reset_lineage import (
    ResetCompareAndSetError,
    ResetLineageStore,
)
from agent_taskflow.reset_lineage_schema import (
    RESET_LINEAGE_MIGRATION,
    migrate_reset_lineage,
)
from agent_taskflow.reset_runtime_path import ResetAwareRuntimeAdmissionStore
from agent_taskflow.store import TaskMirrorStore, connect


class ResetLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_base = self.root / "artifacts" / "AT-PR8-1"
        self.artifact_base.mkdir(parents=True)
        self.task_key = "AT-PR8-1"
        self.task_store = TaskMirrorStore(self.db_path)
        self.task_store.init_db()
        self.task_store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Reset lineage",
                status="blocked",
                repo_path=self.repo,
                artifact_dir=self.artifact_base,
                executor="noop",
                blocked_reason="retry required",
            )
        )
        migrate_reset_lineage(self.db_path)
        attempts = AttemptStore(self.db_path)
        old = attempts.create_attempt(
            self.task_key,
            status="created",
            actor="test",
            reason_code="attempt_created",
        )
        attempts.close_attempt(
            old.attempt_id,
            status="blocked",
            actor="test",
            reason_code="runtime_governance_blocked",
            execution_result="blocked",
        )
        self.old_attempt_id = old.attempt_id
        self.task_store.update_task_status(
            self.task_key,
            "blocked",
            blocked_reason="retry required",
        )

    def test_migration_is_idempotent_and_installs_guards(self) -> None:
        migrate_reset_lineage(self.db_path)
        migrate_reset_lineage(self.db_path)
        with closing(connect(self.db_path)) as conn:
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (RESET_LINEAGE_MIGRATION,),
            ).fetchone()[0]
            triggers = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'trigger' AND name LIKE 'reset_lineage%'
                    """
                )
            }
        self.assertEqual(migration_count, 1)
        self.assertIn("reset_lineage_events_no_update", triggers)
        self.assertIn("reset_lineage_events_no_delete", triggers)
        self.assertIn("reset_lineage_state_guard", triggers)
        self.assertIn("reset_lineage_attempt_task_guard", triggers)

    def test_reset_reserves_exact_new_attempt_and_increments_generation(self) -> None:
        store = ResetLineageStore(self.db_path)
        lineage, replay = store.reserve_retry(
            self.task_key,
            reason="retry after inspection",
            actor="operator",
            request_id="request-one",
            expected_generation=0,
            expected_old_attempt_id=self.old_attempt_id,
        )

        self.assertFalse(replay)
        self.assertEqual(lineage.old_attempt_id, self.old_attempt_id)
        self.assertEqual(lineage.expected_generation, 0)
        self.assertEqual(lineage.committed_generation, 1)
        self.assertEqual(lineage.state, "reserved")
        task = self.task_store.get_task(self.task_key)
        assert task is not None
        self.assertEqual(task.status, "queued")
        with closing(connect(self.db_path)) as conn:
            active_attempt_id = conn.execute(
                "SELECT active_attempt_id FROM tasks WHERE task_key = ?",
                (self.task_key,),
            ).fetchone()[0]
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?",
                (lineage.new_attempt_id,),
            ).fetchone()
            generation = conn.execute(
                "SELECT reset_generation FROM tasks WHERE task_key = ?",
                (self.task_key,),
            ).fetchone()[0]
            active_leases = conn.execute(
                "SELECT COUNT(*) FROM runtime_leases WHERE task_id = ? AND is_active = 1",
                (lineage.task_id,),
            ).fetchone()[0]
        self.assertEqual(active_attempt_id, lineage.new_attempt_id)
        self.assertEqual(attempt["status"], "created")
        self.assertEqual(attempt["attempt_number"], 2)
        self.assertTrue(attempt["is_active"])
        self.assertEqual(generation, 1)
        self.assertEqual(active_leases, 0)

    def test_runtime_claim_adopts_reserved_attempt_without_third_identity(self) -> None:
        lineage, _ = ResetLineageStore(self.db_path).reserve_retry(
            self.task_key,
            reason="retry after inspection",
            actor="operator",
            request_id="request-adopt",
            expected_generation=0,
            expected_old_attempt_id=self.old_attempt_id,
        )
        admission = ResetAwareRuntimeAdmissionStore(self.db_path)
        claim = admission.claim(
            self.task_key,
            owner_id="runtime-owner",
            ttl_seconds=60,
            executor="noop",
            artifact_root=self.artifact_base,
            reason_code="canonical_runtime_pickup_claimed",
        )

        self.assertEqual(claim.attempt_id, lineage.new_attempt_id)
        attempts = AttemptStore(self.db_path).list_attempts(self.task_key)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[-1].status, "preparing")
        claimed = ResetLineageStore(self.db_path).get(lineage.reset_id)
        assert claimed is not None
        self.assertEqual(claimed.state, "claimed")
        self.assertIsNotNone(claimed.claimed_at)
        admission.release(
            claim.attempt_id,
            owner_id=claim.owner_id,
            lease_token=claim.lease_token,
            attempt_status="blocked",
            task_status="blocked",
            reason_code="canonical_runtime_blocked",
            execution_result="blocked",
        )

    def test_two_concurrent_resets_produce_one_winner_and_one_rejection(self) -> None:
        barrier = threading.Barrier(2)

        def reserve(index: int):
            barrier.wait()
            try:
                record, replay = ResetLineageStore(self.db_path).reserve_retry(
                    self.task_key,
                    reason="concurrent retry",
                    actor=f"operator-{index}",
                    request_id=f"concurrent-request-{index}",
                    expected_generation=0,
                    expected_old_attempt_id=self.old_attempt_id,
                )
                return ("ok", record, replay)
            except ResetCompareAndSetError as exc:
                return ("rejected", str(exc), False)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (1, 2)))

        self.assertEqual([item[0] for item in results].count("ok"), 1)
        self.assertEqual([item[0] for item in results].count("rejected"), 1)
        attempts = AttemptStore(self.db_path).list_attempts(self.task_key)
        self.assertEqual(len(attempts), 2)
        with closing(connect(self.db_path)) as conn:
            lineage_count = conn.execute(
                "SELECT COUNT(*) FROM reset_lineages"
            ).fetchone()[0]
            rejection_count = conn.execute(
                """
                SELECT COUNT(*) FROM reset_lineage_events
                WHERE event_type = 'compare_and_set_rejected'
                """
            ).fetchone()[0]
            generation = conn.execute(
                "SELECT reset_generation FROM tasks WHERE task_key = ?",
                (self.task_key,),
            ).fetchone()[0]
        self.assertEqual(lineage_count, 1)
        self.assertEqual(rejection_count, 1)
        self.assertEqual(generation, 1)

    def test_same_request_id_is_idempotent(self) -> None:
        store = ResetLineageStore(self.db_path)
        first, first_replay = store.reserve_retry(
            self.task_key,
            reason="idempotent retry",
            actor="operator",
            request_id="stable-request",
            expected_generation=0,
            expected_old_attempt_id=self.old_attempt_id,
        )
        second, second_replay = store.reserve_retry(
            self.task_key,
            reason="idempotent retry",
            actor="operator",
            request_id="stable-request",
            expected_generation=0,
            expected_old_attempt_id=self.old_attempt_id,
        )
        self.assertFalse(first_replay)
        self.assertTrue(second_replay)
        self.assertEqual(second.reset_id, first.reset_id)
        self.assertEqual(second.new_attempt_id, first.new_attempt_id)
        self.assertEqual(len(AttemptStore(self.db_path).list_attempts(self.task_key)), 2)

    def test_reset_lineage_events_are_append_only(self) -> None:
        lineage, _ = ResetLineageStore(self.db_path).reserve_retry(
            self.task_key,
            reason="immutable audit",
            actor="operator",
            request_id="immutable-request",
            expected_generation=0,
        )
        with closing(connect(self.db_path)) as conn:
            event_id = conn.execute(
                "SELECT event_id FROM reset_lineage_events WHERE reset_id = ?",
                (lineage.reset_id,),
            ).fetchone()[0]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "reset lineage events are append-only",
            ):
                with conn:
                    conn.execute(
                        "UPDATE reset_lineage_events SET actor = 'rewrite' WHERE event_id = ?",
                        (event_id,),
                    )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "reset lineage events are append-only",
            ):
                with conn:
                    conn.execute(
                        "DELETE FROM reset_lineage_events WHERE event_id = ?",
                        (event_id,),
                    )


if __name__ == "__main__":
    unittest.main()
