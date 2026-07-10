from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord, utc_now_iso
from agent_taskflow.runtime_admission import (
    LeaseExpiredError,
    LeaseOwnershipError,
    RuntimeAdmissionStore,
)
from agent_taskflow.runtime_admission_schema import (
    RUNTIME_ADMISSION_MIGRATION,
    migrate_runtime_admission,
)
from agent_taskflow.store import TaskMirrorStore, connect


REPO_ROOT = Path(__file__).resolve().parents[1]


class RuntimeAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._insert_task("AT-PR3-1")
        migrate_runtime_admission(self.db_path)
        self.admission = RuntimeAdmissionStore(self.db_path)
        self.attempts = AttemptStore(self.db_path)

    def _insert_task(self, task_key: str, *, status: str = "queued") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id=f"task-{task_key.lower()}",
                title=f"Runtime admission {task_key}",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=artifact_dir,
                executor="noop",
            )
        )

    def _set_lease_expired(self, lease_id: str) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE runtime_leases
                SET expires_at = '2000-01-01T00:00:00Z'
                WHERE lease_id = ?
                """,
                (lease_id,),
            )

    def test_migration_is_idempotent_and_installs_required_guards(self) -> None:
        migrate_runtime_admission(self.db_path)
        migrate_runtime_admission(self.db_path)
        with closing(connect(self.db_path)) as conn, conn:
            migration_count = conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE name = ?",
                (RUNTIME_ADMISSION_MIGRATION,),
            ).fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
        self.assertEqual(migration_count, 1)
        self.assertIn("runtime_leases", tables)
        self.assertIn("runtime_claim_suppressions", tables)
        for trigger in (
            "runtime_duplicate_pickup_guard",
            "runtime_pickup_claim_after_preparing",
            "runtime_transition_requires_live_lease",
            "runtime_executor_start_requires_live_lease",
            "runtime_task_event_heartbeat",
            "runtime_terminal_status_releases_lease",
        ):
            self.assertIn(trigger, triggers)

    def test_migration_does_not_synthesize_attempt_before_runtime_pickup(self) -> None:
        self.assertEqual(self.attempts.list_attempts("AT-PR3-1"), [])
        self.assertIsNone(self.admission.get_active_lease("AT-PR3-1"))

    def test_legacy_preparing_transition_atomically_claims_attempt_and_lease(self) -> None:
        self.store.update_task_status(
            "AT-PR3-1",
            "preparing",
            source="approved_task_runner",
            message="Preparing workspace",
        )
        attempt = self.attempts.get_active_attempt("AT-PR3-1")
        lease = self.admission.get_active_lease("AT-PR3-1")
        task = self.store.get_task("AT-PR3-1")
        self.assertIsNotNone(attempt)
        self.assertIsNotNone(lease)
        self.assertIsNotNone(task)
        assert attempt is not None and lease is not None and task is not None
        self.assertEqual(attempt.status, "preparing")
        self.assertEqual(lease.attempt_id, attempt.attempt_id)
        self.assertEqual(lease.auth_mode, "implicit_status")
        self.assertRegex(lease.owner_id, r"^approved_task_runner:event-\d+$")
        identity = self.attempts.get_task_identity("AT-PR3-1")
        assert identity is not None
        self.assertEqual(identity.active_attempt_id, attempt.attempt_id)

    def test_second_runtime_pickup_is_rejected(self) -> None:
        self.store.update_task_status("AT-PR3-1", "preparing", source="dispatcher")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "already claimed"):
            self.store.update_task_status(
                "AT-PR3-1",
                "preparing",
                source="approved_task_runner",
            )
        self.assertEqual(len(self.attempts.list_attempts("AT-PR3-1")), 1)

    def test_concurrent_runtime_pickup_has_exactly_one_winner(self) -> None:
        self._insert_task("AT-PR3-RACE")

        def claim(source: str) -> str:
            local_store = TaskMirrorStore(self.db_path)
            try:
                local_store.update_task_status(
                    "AT-PR3-RACE",
                    "preparing",
                    source=source,
                )
                return "claimed"
            except sqlite3.IntegrityError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("dispatcher-a", "dispatcher-b")))
        self.assertEqual(sorted(results), ["claimed", "rejected"])
        attempts = self.attempts.list_attempts("AT-PR3-RACE")
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].is_active)

    def test_executor_start_event_without_lease_fails_closed(self) -> None:
        self._insert_task("AT-PR3-NO-LEASE")
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "executor start requires active unexpired runtime lease",
        ):
            self.store.create_executor_run("AT-PR3-NO-LEASE", "noop")

    def test_implementing_transition_without_lease_fails_closed(self) -> None:
        self._insert_task("AT-PR3-NO-TRANSITION")
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "runtime transition requires active unexpired lease",
        ):
            self.store.update_task_status(
                "AT-PR3-NO-TRANSITION",
                "implementing",
                source="dispatcher",
            )

    def test_persisted_runtime_events_refresh_implicit_heartbeat(self) -> None:
        self.store.update_task_status("AT-PR3-1", "preparing", source="dispatcher")
        lease = self.admission.get_active_lease("AT-PR3-1")
        assert lease is not None
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE runtime_leases
                SET heartbeat_at = '2000-01-01T00:00:00Z',
                    expires_at = '2999-01-01T00:00:00Z'
                WHERE lease_id = ?
                """,
                (lease.lease_id,),
            )
        self.store.record_task_event(
            "AT-PR3-1",
            "note",
            "dispatcher",
            message="Executor progress boundary",
            payload={"kind": "runtime_progress"},
        )
        refreshed = self.admission.get_active_lease("AT-PR3-1")
        assert refreshed is not None
        self.assertNotEqual(refreshed.heartbeat_at, "2000-01-01T00:00:00Z")
        self.assertLess(refreshed.heartbeat_at, refreshed.expires_at)

    def test_expired_lease_blocks_transition_and_executor_start(self) -> None:
        self.store.update_task_status("AT-PR3-1", "preparing", source="dispatcher")
        lease = self.admission.get_active_lease("AT-PR3-1")
        assert lease is not None
        self._set_lease_expired(lease.lease_id)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "active unexpired lease"):
            self.store.update_task_status(
                "AT-PR3-1",
                "implementing",
                source="dispatcher",
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "active unexpired"):
            self.store.create_executor_run("AT-PR3-1", "noop")
        with self.assertRaises(LeaseExpiredError):
            self.admission.assert_executor_start_allowed("AT-PR3-1")

    def test_terminal_task_status_releases_implicit_lease(self) -> None:
        self.store.update_task_status("AT-PR3-1", "preparing", source="dispatcher")
        attempt = self.attempts.get_active_attempt("AT-PR3-1")
        lease = self.admission.get_active_lease("AT-PR3-1")
        assert attempt is not None and lease is not None
        self.store.update_task_status(
            "AT-PR3-1",
            "waiting_approval",
            source="dispatcher",
        )
        self.assertIsNone(self.attempts.get_active_attempt("AT-PR3-1"))
        self.assertIsNone(self.admission.get_active_lease("AT-PR3-1"))
        closed_attempt = self.attempts.get_attempt(attempt.attempt_id)
        closed_lease = self.admission.get_lease(lease.lease_id)
        assert closed_attempt is not None and closed_lease is not None
        self.assertFalse(closed_attempt.is_active)
        self.assertEqual(closed_attempt.status, "waiting_approval")
        self.assertFalse(closed_lease.is_active)
        self.assertEqual(closed_lease.release_reason, "task_status:waiting_approval")

    def test_explicit_claim_heartbeat_and_release_require_owner_token(self) -> None:
        claim = self.admission.claim(
            "AT-PR3-1",
            owner_id="runner-instance-1",
            ttl_seconds=60,
            executor="noop",
        )
        lease = self.admission.get_active_lease("AT-PR3-1")
        assert lease is not None
        self.assertEqual(lease.auth_mode, "token")
        self.assertEqual(lease.owner_id, "runner-instance-1")
        with closing(connect(self.db_path)) as conn, conn:
            stored_hash = conn.execute(
                "SELECT token_hash FROM runtime_leases WHERE lease_id = ?",
                (claim.lease_id,),
            ).fetchone()[0]
        self.assertNotEqual(stored_hash, claim.lease_token)
        self.assertRegex(stored_hash, r"^[0-9a-f]{64}$")
        with self.assertRaises(LeaseOwnershipError):
            self.admission.heartbeat(
                claim.attempt_id,
                owner_id="runner-instance-1",
                lease_token="wrong-token",
            )
        heartbeat = self.admission.heartbeat(
            claim.attempt_id,
            owner_id="runner-instance-1",
            lease_token=claim.lease_token,
            ttl_seconds=120,
        )
        self.assertGreaterEqual(heartbeat.ttl_seconds, 120)
        released = self.admission.release(
            claim.attempt_id,
            owner_id="runner-instance-1",
            lease_token=claim.lease_token,
            attempt_status="waiting_approval",
            task_status="waiting_approval",
            reason_code="implementation_validated",
            execution_result="completed",
            validation_result="passed",
        )
        self.assertFalse(released.is_active)
        task = self.store.get_task("AT-PR3-1")
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        identity = self.attempts.get_task_identity("AT-PR3-1")
        assert identity is not None
        self.assertIsNone(identity.active_attempt_id)

    def test_stale_lease_reaper_aborts_attempt_and_blocks_task(self) -> None:
        claim = self.admission.claim(
            "AT-PR3-1",
            owner_id="crashed-runner",
            ttl_seconds=60,
        )
        self._set_lease_expired(claim.lease_id)
        reaped = self.admission.expire_stale_leases()
        self.assertEqual(reaped, [claim.attempt_id])
        task = self.store.get_task("AT-PR3-1")
        attempt = self.attempts.get_attempt(claim.attempt_id)
        lease = self.admission.get_lease(claim.lease_id)
        assert task is not None and attempt is not None and lease is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "runtime_lease_expired")
        self.assertEqual(attempt.status, "execution_aborted")
        self.assertFalse(attempt.is_active)
        self.assertFalse(lease.is_active)
        self.assertEqual(lease.release_reason, "runtime_lease_expired")

    def test_sqlite_rejects_second_active_lease_for_task(self) -> None:
        self.store.update_task_status("AT-PR3-1", "preparing", source="dispatcher")
        lease = self.admission.get_active_lease("AT-PR3-1")
        assert lease is not None
        with closing(connect(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        """
                        INSERT INTO runtime_leases(
                            lease_id, task_id, attempt_id, owner_id, token_hash,
                            auth_mode, ttl_seconds, acquired_at, heartbeat_at,
                            expires_at, is_active
                        )
                        VALUES (
                            'duplicate-lease', ?, ?, 'other-owner',
                            'duplicate-token-hash', 'implicit_status', 60,
                            ?, ?, '2999-01-01T00:00:00Z', 1
                        )
                        """,
                        (
                            lease.task_id,
                            lease.attempt_id,
                            utc_now_iso(),
                            utc_now_iso(),
                        ),
                    )

    def test_lifecycle_contains_claim_heartbeat_and_release_evidence(self) -> None:
        claim = self.admission.claim("AT-PR3-1", owner_id="runner-evidence")
        self.admission.heartbeat(
            claim.attempt_id,
            owner_id="runner-evidence",
            lease_token=claim.lease_token,
        )
        self.admission.release(
            claim.attempt_id,
            owner_id="runner-evidence",
            lease_token=claim.lease_token,
            attempt_status="blocked",
            task_status="blocked",
            reason_code="operator_abort",
        )
        events = self.attempts.list_lifecycle_events("AT-PR3-1")
        reason_codes = [event.reason_code for event in events]
        self.assertIn("runtime_pickup_claimed", reason_codes)
        self.assertIn("runtime_lease_heartbeat", reason_codes)
        self.assertIn("operator_abort", reason_codes)

    def test_current_executor_call_sites_are_covered_by_persisted_admission(self) -> None:
        call_sites: set[str] = set()
        for path in (REPO_ROOT / "agent_taskflow").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bexecutor\.run\(", text):
                call_sites.add(path.relative_to(REPO_ROOT).as_posix())
                self.assertIn("create_executor_run(", text)
                self.assertIn('"preparing"', text)
        self.assertEqual(
            call_sites,
            {
                "agent_taskflow/approved_task_runner.py",
                "agent_taskflow/dispatcher.py",
            },
        )
        queued_handoff = (
            REPO_ROOT / "agent_taskflow" / "queued_task_handoff.py"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(queued_handoff, r"\bexecutor\.run\(")
        self.assertIn("run_approved_task", queued_handoff)


if __name__ == "__main__":
    unittest.main()
