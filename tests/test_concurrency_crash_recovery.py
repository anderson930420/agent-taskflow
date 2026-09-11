"""Crash-recovery rehearsal (V1 Step 4, SPEC §19.3).

A real runtime process holding a lease through the dispatcher's ``preparing``
path is killed with SIGKILL mid-run. The lease must expire on schedule, the
reaper must recover the Ticket, the existing retry path must bring it back, no
Ticket may stay running forever, ownership may never be doubled, and the
killed Attempt must stay readable with its events.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.concurrency_rehearsal import (
    add_rehearsal_task,
    create_rehearsal_fixture,
    dispatcher_preparing_claim,
    ownership_violations,
    read_worker_line,
    start_worker_process,
)
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.runtime_reaper import reap_stale_runtime, running_without_live_lease
from agent_taskflow.store import TaskMirrorStore, connect

REPO_ROOT = Path(__file__).resolve().parents[1]
LEASE_TTL_SECONDS = 2


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class SigkilledLeaseHolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fixture = create_rehearsal_fixture(Path(cls.tmp.name) / "fixture")
        add_rehearsal_task(cls.fixture, "AT-CRASH-1")
        db = cls.fixture.db_path
        cls.checkpoints: dict[str, object] = {}

        cls.process = start_worker_process(
            {
                "op": "crash-holder",
                "db_path": str(db),
                "task_key": "AT-CRASH-1",
                "owner_id": "doomed-runtime",
                "lease_ttl_seconds": LEASE_TTL_SECONDS,
            }
        )
        cls.holder = read_worker_line(cls.process, timeout=60)
        # Outlive one TTL: only the holder's heartbeat thread can keep the
        # lease live that long.
        time.sleep(LEASE_TTL_SECONDS + 1)
        cls.lease_before_kill = RuntimeAdmissionStore(db).get_lease(cls.holder["lease_id"])
        cls.kill_wallclock = datetime.now(timezone.utc)
        os.kill(cls.process.pid, signal.SIGKILL)
        cls.return_code = cls.process.wait(timeout=30)
        cls.killed_at = time.monotonic()

        cls.early_reap = reap_stale_runtime(db)
        cls.early_lease = RuntimeAdmissionStore(db).get_lease(cls.holder["lease_id"])
        cls.early_violations = ownership_violations(db)

        remaining = LEASE_TTL_SECONDS + 0.5 - (time.monotonic() - cls.killed_at)
        if remaining > 0:
            time.sleep(remaining)
        cls.running_before_reap = running_without_live_lease(db)
        cls.reap = reap_stale_runtime(db)
        cls.second_reap = reap_stale_runtime(db)
        cls.running_after_reap = running_without_live_lease(db)
        cls.violations_after_reap = ownership_violations(db)

        cls.reset = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "reset_task_status.py"),
                "--task-key",
                "AT-CRASH-1",
                "--db-path",
                str(db),
                "--from-status",
                "blocked",
                "--reason",
                "SIGKILLed runtime recovered by Step 4 rehearsal",
                "--actor",
                "step4-test-operator",
                "--confirm-reset",
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cls.retry = dispatcher_preparing_claim(db, "AT-CRASH-1", source="recovered-runtime")
        cls.violations_after_retry = ownership_violations(db)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.process.poll() is None:  # pragma: no cover - defensive
            cls.process.kill()
            cls.process.wait()
        cls.tmp.cleanup()

    def test_the_holder_was_a_real_process_killed_by_sigkill(self) -> None:
        self.assertNotEqual(self.holder["pid"], os.getpid())
        self.assertEqual(self.return_code, -signal.SIGKILL)
        self.assertEqual(self.holder["task_status"], "implementing")

    def test_heartbeat_kept_the_lease_alive_until_the_kill(self) -> None:
        self.assertIsNotNone(self.lease_before_kill)
        self.assertTrue(self.lease_before_kill.is_active)
        acquired = _parse_utc(self.holder["acquired_at"])
        expires = _parse_utc(self.lease_before_kill.expires_at)
        self.assertGreater(
            (self.kill_wallclock - acquired).total_seconds(), LEASE_TTL_SECONDS
        )
        self.assertGreater(expires, self.kill_wallclock)

    def test_lease_is_not_reaped_before_it_expires(self) -> None:
        self.assertEqual(self.early_reap.expired_attempt_ids, ())
        self.assertIsNotNone(self.early_lease)
        self.assertTrue(self.early_lease.is_active)
        self.assertEqual(self.early_violations, [])

    def test_lease_expires_and_is_reaped_once(self) -> None:
        self.assertEqual(self.reap.expired_attempt_ids, (self.holder["attempt_id"],))
        lease = RuntimeAdmissionStore(self.fixture.db_path).get_lease(
            self.holder["lease_id"]
        )
        self.assertFalse(lease.is_active)
        self.assertEqual(lease.release_reason, "runtime_lease_expired")

    def test_reaper_is_idempotent(self) -> None:
        self.assertEqual(self.second_reap.expired_attempt_ids, ())
        self.assertEqual(self.second_reap.reaped_resource_attempt_ids, ())

    def test_dead_process_markers_are_reaped(self) -> None:
        self.assertEqual(
            self.reap.reaped_resource_attempt_ids, (self.holder["attempt_id"],)
        )
        self.assertEqual(self.reap.blocked_live_pid_attempt_ids, ())
        self.assertFalse(Path(self.holder["pid_path"]).exists())
        with closing(connect(self.fixture.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM attempt_resources WHERE attempt_id = ?",
                (self.holder["attempt_id"],),
            ).fetchone()[0]
        self.assertEqual(status, "reaped")

    def test_nothing_stays_running_forever(self) -> None:
        self.assertEqual(self.running_before_reap, ["AT-CRASH-1"])
        self.assertEqual(self.running_after_reap, [])
        task = TaskMirrorStore(self.fixture.db_path).get_task("AT-CRASH-1")
        self.assertEqual(task.status, "preparing")  # the retry's new claim

    def test_ticket_is_recoverable_through_the_existing_retry_path(self) -> None:
        self.assertEqual(self.reset.returncode, 0, self.reset.stderr)
        payload = json.loads(self.reset.stdout)
        self.assertEqual(payload["old_attempt_id"], self.holder["attempt_id"])
        self.assertEqual(self.retry["outcome"], "claimed", self.retry)
        self.assertEqual(self.retry["attempt_id"], payload["new_attempt_id"])
        self.assertNotEqual(self.retry["attempt_id"], self.holder["attempt_id"])

    def test_no_double_ownership_at_any_checkpoint(self) -> None:
        self.assertEqual(self.violations_after_reap, [])
        self.assertEqual(self.violations_after_retry, [])
        with closing(connect(self.fixture.db_path)) as conn:
            active = conn.execute(
                "SELECT attempt_id, owner_id FROM runtime_leases "
                "JOIN tasks ON tasks.task_id = runtime_leases.task_id "
                "WHERE tasks.task_key = 'AT-CRASH-1' AND runtime_leases.is_active = 1"
            ).fetchall()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["attempt_id"], self.retry["attempt_id"])
        self.assertNotEqual(active[0]["owner_id"], self.holder["owner_id"])

    def test_killed_owner_token_is_refused_after_recovery(self) -> None:
        admission = RuntimeAdmissionStore(self.fixture.db_path)
        from agent_taskflow.runtime_admission import LeaseOwnershipError

        with self.assertRaises(LeaseOwnershipError):
            admission.heartbeat(
                self.holder["attempt_id"],
                owner_id=self.holder["owner_id"],
                lease_token=self.holder["lease_token"],
            )
        with self.assertRaises(LeaseOwnershipError):
            admission.release(
                self.holder["attempt_id"],
                owner_id=self.holder["owner_id"],
                lease_token=self.holder["lease_token"],
                attempt_status="waiting_approval",
                task_status="waiting_approval",
                reason_code="late_release",
            )

    def test_previous_attempt_remains_auditable(self) -> None:
        attempts = AttemptStore(self.fixture.db_path)
        old = attempts.get_attempt(self.holder["attempt_id"])
        self.assertEqual(old.status, "execution_aborted")
        self.assertFalse(old.is_active)
        self.assertEqual(old.execution_result, "lease_expired")
        events = [
            event
            for event in attempts.list_lifecycle_events("AT-CRASH-1")
            if event.attempt_id == self.holder["attempt_id"]
        ]
        reasons = [event.reason_code for event in events]
        self.assertEqual(reasons[0], "canonical_runtime_pickup_claimed")
        self.assertIn("runtime_lease_heartbeat", reasons)
        self.assertIn("runtime_implementing", reasons)
        self.assertEqual(reasons[-1], "runtime_lease_expired")
        self.assertEqual(events[-1].actor, "runtime_lease_reaper")
        steps = RuntimeProgressStore(self.fixture.db_path).list_steps(
            self.holder["attempt_id"]
        )
        self.assertEqual(
            [(step.name, step.status) for step in steps],
            [("Prepare", "passed"), ("Implementer", "running")],
        )
        all_attempts = attempts.list_attempts("AT-CRASH-1")
        self.assertEqual(
            [attempt.attempt_number for attempt in all_attempts], [1, 2]
        )


class ReaperCliTests(unittest.TestCase):
    def test_cli_reaps_expired_leases_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = create_rehearsal_fixture(Path(tmp) / "fixture")
            add_rehearsal_task(fixture, "AT-REAP-CLI")
            admission = RuntimeAdmissionStore(fixture.db_path)
            claim = admission.claim("AT-REAP-CLI", owner_id="cli-crash", ttl_seconds=60)
            with closing(connect(fixture.db_path)) as conn, conn:
                conn.execute(
                    "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' "
                    "WHERE lease_id = ?",
                    (claim.lease_id,),
                )

            def run_cli() -> dict:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-S",
                        str(REPO_ROOT / "scripts" / "reap_stale_runtime.py"),
                        "--db-path",
                        str(fixture.db_path),
                    ],
                    cwd=REPO_ROOT,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                return json.loads(completed.stdout)

            first = run_cli()
            second = run_cli()
        self.assertEqual(first["expired_attempt_ids"], [claim.attempt_id])
        self.assertEqual(second["expired_attempt_ids"], [])
        self.assertEqual(first["running_without_live_lease"], [])
        self.assertFalse(first["daemon"])
        self.assertFalse(first["historical_worktrees_deleted"])

    def test_cli_requires_an_explicit_db_path(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(REPO_ROOT / "scripts" / "reap_stale_runtime.py")],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--db-path", completed.stderr)

    def test_reaper_installs_no_schema_on_a_bare_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bare.db"
            TaskMirrorStore(db).init_db()
            with closing(connect(db)) as conn:
                before = sorted(
                    row[0] for row in conn.execute("SELECT name FROM sqlite_master")
                )
            result = reap_stale_runtime(db)
            with closing(connect(db)) as conn:
                after = sorted(
                    row[0] for row in conn.execute("SELECT name FROM sqlite_master")
                )
        self.assertEqual(before, after)
        self.assertEqual(result.expired_attempt_ids, ())
        self.assertIn("runtime_leases", " ".join(result.skipped))


if __name__ == "__main__":
    unittest.main()
