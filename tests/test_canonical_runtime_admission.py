from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import agent_taskflow.approved_task_runner as approved_task_runner_module
import agent_taskflow.dispatcher as dispatcher_module
import agent_taskflow.runtime_admission as runtime_admission_module
from agent_taskflow.canonical_runtime_path import (
    CANONICAL_RUNTIME_ADMISSION_MIGRATION,
    CanonicalRuntimeAdmissionStore,
    CanonicalRuntimeTaskStore,
    migrate_canonical_runtime_admission,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class CanonicalRuntimeAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.artifact_dir = self.root / "artifacts" / "AT-CANONICAL-1"
        self.base_store = TaskMirrorStore(self.db_path)
        self.base_store.init_db()
        self.base_store.upsert_task(
            TaskRecord(
                task_key="AT-CANONICAL-1",
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
            )
        )

    def canonical_store(
        self,
        *,
        ttl: int = 30,
        heartbeat_interval: float = 60.0,
    ) -> CanonicalRuntimeTaskStore:
        store = CanonicalRuntimeTaskStore(
            self.db_path,
            lease_ttl_seconds=ttl,
            heartbeat_interval_seconds=heartbeat_interval,
        )
        self.addCleanup(store.shutdown_runtime_supervisors)
        return store

    def test_bootstrap_installs_canonical_direct_entrypoints(self) -> None:
        self.assertTrue(
            getattr(approved_task_runner_module.run_approved_task, "__canonical_runtime__", False)
        )
        self.assertTrue(getattr(dispatcher_module.Dispatcher, "__canonical_runtime__", False))
        self.assertIs(
            runtime_admission_module.RuntimeAdmissionStore,
            CanonicalRuntimeAdmissionStore,
        )

        import agent_taskflow.github_issue_one_task_scheduler_tick as scheduler_tick
        import agent_taskflow.queued_task_handoff as queued_task_handoff

        self.assertTrue(
            getattr(queued_task_handoff.run_approved_task, "__canonical_runtime__", False)
        )
        self.assertTrue(
            getattr(scheduler_tick.run_approved_task, "__canonical_runtime__", False)
        )

    def test_migration_removes_implicit_pickup_and_records_version(self) -> None:
        migrate_canonical_runtime_admission(self.db_path)
        migrate_canonical_runtime_admission(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            recorded = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (CANONICAL_RUNTIME_ADMISSION_MIGRATION,),
            ).fetchone()

        self.assertIsNotNone(recorded)
        self.assertNotIn("runtime_pickup_claim_after_preparing", names)
        self.assertNotIn("runtime_task_event_heartbeat", names)
        self.assertNotIn("runtime_terminal_status_releases_lease", names)
        self.assertIn("runtime_preparing_requires_canonical_claim", names)
        self.assertIn("runtime_executor_start_requires_canonical_claim", names)
        self.assertIn("runtime_token_terminal_requires_owned_release", names)

    def test_plain_store_cannot_pick_up_task_after_migration(self) -> None:
        migrate_canonical_runtime_admission(self.db_path)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "canonical runtime admission claim required",
        ):
            self.base_store.update_task_status(
                "AT-CANONICAL-1",
                "preparing",
                source="legacy_runner",
            )

        task = self.base_store.get_task("AT-CANONICAL-1")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

    def test_canonical_preparing_transition_creates_explicit_claim(self) -> None:
        store = self.canonical_store()
        store.update_task_status(
            "AT-CANONICAL-1",
            "preparing",
            source="approved_task_runner",
            message="claim task",
        )

        claim = store.runtime_claim("AT-CANONICAL-1")
        lease = CanonicalRuntimeAdmissionStore(self.db_path).get_active_lease(
            "AT-CANONICAL-1"
        )
        self.assertIsNotNone(claim)
        self.assertIsNotNone(lease)
        assert claim is not None
        assert lease is not None
        self.assertEqual(lease.auth_mode, "token")
        self.assertEqual(lease.attempt_id, claim.attempt_id)
        self.assertEqual(lease.lease_id, claim.lease_id)
        self.assertEqual(lease.owner_id, claim.owner_id)
        self.assertTrue(claim.owner_id.startswith("approved_task_runner:"))

        with sqlite3.connect(self.db_path) as conn:
            token_hash = conn.execute(
                "SELECT token_hash FROM runtime_leases WHERE lease_id = ?",
                (claim.lease_id,),
            ).fetchone()[0]
        self.assertNotEqual(token_hash, claim.lease_token)
        self.assertNotIn(claim.lease_token, token_hash)

    def test_base_executor_event_cannot_piggyback_on_token_lease(self) -> None:
        store = self.canonical_store()
        store.update_task_status(
            "AT-CANONICAL-1",
            "preparing",
            source="dispatcher",
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "executor start requires canonical runtime claim metadata",
        ):
            self.base_store.create_executor_run("AT-CANONICAL-1", "noop")

        run_id = store.create_executor_run("AT-CANONICAL-1", "noop")
        events = self.base_store.list_task_events("AT-CANONICAL-1")
        payloads = [
            json.loads(event.payload_json)
            for event in events
            if event.payload_json and "executor_run_started" in event.payload_json
        ]
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        claim = store.runtime_claim("AT-CANONICAL-1")
        assert claim is not None
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["runtime_attempt_id"], claim.attempt_id)
        self.assertEqual(payload["runtime_lease_id"], claim.lease_id)
        self.assertEqual(payload["runtime_owner_id"], claim.owner_id)
        self.assertNotIn("lease_token", payload)

    def test_token_terminal_transition_requires_owned_release(self) -> None:
        store = self.canonical_store()
        store.update_task_status(
            "AT-CANONICAL-1",
            "preparing",
            source="approved_task_runner",
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "token runtime lease requires owned release",
        ):
            self.base_store.update_task_status(
                "AT-CANONICAL-1",
                "blocked",
                source="bypass",
                blocked_reason="bypass",
            )

        store.update_task_status(
            "AT-CANONICAL-1",
            "blocked",
            source="approved_task_runner",
            message="executor failed",
            blocked_reason="executor failed",
        )
        task = self.base_store.get_task("AT-CANONICAL-1")
        lease = CanonicalRuntimeAdmissionStore(self.db_path).get_active_lease(
            "AT-CANONICAL-1"
        )
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "executor failed")
        self.assertIsNone(task.__dict__.get("active_attempt_id"))
        self.assertIsNone(lease)
        self.assertIsNone(store.runtime_claim("AT-CANONICAL-1"))

    def test_owned_heartbeat_extends_lease(self) -> None:
        store = self.canonical_store(ttl=5, heartbeat_interval=60.0)
        store.update_task_status(
            "AT-CANONICAL-1",
            "preparing",
            source="dispatcher",
        )
        admission = CanonicalRuntimeAdmissionStore(self.db_path)
        before = admission.get_active_lease("AT-CANONICAL-1")
        self.assertIsNotNone(before)
        time.sleep(1.05)
        store.update_task_status(
            "AT-CANONICAL-1",
            "implementing",
            source="dispatcher",
        )
        after = admission.get_active_lease("AT-CANONICAL-1")
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        self.assertGreater(after.heartbeat_at, before.heartbeat_at)
        self.assertGreater(after.expires_at, before.expires_at)

    def test_migration_refuses_active_implicit_lease(self) -> None:
        from agent_taskflow.runtime_admission_schema import migrate_runtime_admission

        migrate_runtime_admission(self.db_path)
        self.base_store.update_task_status(
            "AT-CANONICAL-1",
            "preparing",
            source="legacy_runner",
        )

        with self.assertRaisesRegex(RuntimeError, "active implicit_status leases exist"):
            migrate_canonical_runtime_admission(self.db_path)


if __name__ == "__main__":
    unittest.main()
