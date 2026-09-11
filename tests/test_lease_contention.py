"""Lease contention (V1 Step 4, §42 "Lease contention"; §44 one active owner).

Heartbeat racing the reaper, a late heartbeat or release from an expired
owner after a new owner claimed, and many owners contending for one lease.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path

from agent_taskflow.concurrency_rehearsal import (
    add_rehearsal_task,
    create_rehearsal_fixture,
    explicit_claim,
    ownership_violations,
    run_thread_race,
)
from agent_taskflow.reset_lineage import ResetLineageStore
from agent_taskflow.runtime_admission import (
    LeaseExpiredError,
    LeaseOwnershipError,
    RuntimeAdmissionStore,
)
from agent_taskflow.runtime_capacity import set_disposable_fixture_capacity
from agent_taskflow.runtime_reaper import reap_stale_runtime
from agent_taskflow.store import connect

ROUNDS = 12


class LeaseContentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = create_rehearsal_fixture(Path(self.tmp.name) / "fixture")
        self.admission = RuntimeAdmissionStore(self.fixture.db_path)

    def expire(self, lease_id: str) -> None:
        with closing(connect(self.fixture.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE lease_id = ?",
                (lease_id,),
            )

    def lease_rows(self, task_key: str) -> list:
        with closing(connect(self.fixture.db_path)) as conn:
            return conn.execute(
                "SELECT runtime_leases.* FROM runtime_leases "
                "JOIN tasks ON tasks.task_id = runtime_leases.task_id "
                "WHERE tasks.task_key = ? ORDER BY acquired_at, lease_id",
                (task_key,),
            ).fetchall()

    def recover(self, task_key: str, old_attempt_id: str) -> None:
        ResetLineageStore(self.fixture.db_path).reserve_retry(
            task_key,
            reason="lease contention rehearsal",
            actor="step4-test",
            expected_old_attempt_id=old_attempt_id,
        )


class HeartbeatVersusReaperTests(LeaseContentionTestCase):
    def test_expired_lease_heartbeat_never_resurrects_it(self) -> None:
        for round_index in range(ROUNDS):
            task_key = f"AT-HB-EXP-{round_index}"
            add_rehearsal_task(self.fixture, task_key)
            claim = self.admission.claim(task_key, owner_id="owner", ttl_seconds=60)
            self.expire(claim.lease_id)
            barrier = threading.Barrier(2)

            def heartbeat() -> str:
                barrier.wait()
                try:
                    RuntimeAdmissionStore(self.fixture.db_path).heartbeat(
                        claim.attempt_id,
                        owner_id="owner",
                        lease_token=claim.lease_token,
                    )
                except (LeaseExpiredError, LeaseOwnershipError) as exc:
                    return type(exc).__name__
                return "extended"

            def reap() -> tuple[str, ...]:
                barrier.wait()
                return reap_stale_runtime(self.fixture.db_path).expired_attempt_ids

            with ThreadPoolExecutor(max_workers=2) as pool:
                heartbeat_future = pool.submit(heartbeat)
                reap_future = pool.submit(reap)
            self.assertIn(
                heartbeat_future.result(), {"LeaseExpiredError", "LeaseOwnershipError"}
            )
            self.assertEqual(reap_future.result(), (claim.attempt_id,))
            lease = self.admission.get_lease(claim.lease_id)
            self.assertFalse(lease.is_active)
            self.assertEqual(lease.release_reason, "runtime_lease_expired")

    def test_live_lease_heartbeat_wins_and_the_reaper_leaves_it(self) -> None:
        # Every round's live lease stays active.
        set_disposable_fixture_capacity(self.fixture.db_path, ROUNDS, fixture=self.id())
        for round_index in range(ROUNDS):
            task_key = f"AT-HB-LIVE-{round_index}"
            add_rehearsal_task(self.fixture, task_key)
            claim = self.admission.claim(task_key, owner_id="owner", ttl_seconds=60)
            barrier = threading.Barrier(2)

            def heartbeat() -> str:
                barrier.wait()
                RuntimeAdmissionStore(self.fixture.db_path).heartbeat(
                    claim.attempt_id, owner_id="owner", lease_token=claim.lease_token
                )
                return "extended"

            def reap() -> tuple[str, ...]:
                barrier.wait()
                return reap_stale_runtime(self.fixture.db_path).expired_attempt_ids

            with ThreadPoolExecutor(max_workers=2) as pool:
                heartbeat_future = pool.submit(heartbeat)
                reap_future = pool.submit(reap)
            self.assertEqual(heartbeat_future.result(), "extended")
            self.assertEqual(reap_future.result(), ())
            self.assertTrue(self.admission.get_lease(claim.lease_id).is_active)

    def test_racing_reapers_reap_each_lease_exactly_once(self) -> None:
        # Four expired, unreaped leases each keep their slot until reaped.
        set_disposable_fixture_capacity(self.fixture.db_path, 4, fixture=self.id())
        claims = []
        for index in range(4):
            task_key = f"AT-REAPERS-{index}"
            add_rehearsal_task(self.fixture, task_key)
            claim = self.admission.claim(task_key, owner_id=f"o{index}", ttl_seconds=60)
            self.expire(claim.lease_id)
            claims.append(claim)
        outcomes = run_thread_race(
            [
                (lambda: {"outcome": "done", "reaped": list(
                    reap_stale_runtime(self.fixture.db_path).expired_attempt_ids
                )})
                for _ in range(6)
            ]
        )
        reaped = [attempt for item in outcomes for attempt in item["reaped"]]
        self.assertEqual(sorted(reaped), sorted(claim.attempt_id for claim in claims))
        with closing(connect(self.fixture.db_path)) as conn:
            reaper_events = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_events WHERE reason_code = 'runtime_lease_expired'"
            ).fetchone()[0]
        self.assertEqual(reaper_events, len(claims))


class StaleOwnerAfterReclaimTests(LeaseContentionTestCase):
    def setUp(self) -> None:
        super().setUp()
        add_rehearsal_task(self.fixture, "AT-STALE")
        self.old = self.admission.claim("AT-STALE", owner_id="old-owner", ttl_seconds=60)
        self.expire(self.old.lease_id)
        reap_stale_runtime(self.fixture.db_path)
        self.recover("AT-STALE", self.old.attempt_id)
        new = explicit_claim_via_runtime(self.fixture.db_path, "AT-STALE", "new-owner")
        self.new = new

    def test_stale_heartbeat_is_refused_and_the_new_lease_is_untouched(self) -> None:
        before = self.admission.get_lease(self.new.lease_id)
        with self.assertRaises(LeaseOwnershipError):
            self.admission.heartbeat(
                self.old.attempt_id,
                owner_id="old-owner",
                lease_token=self.old.lease_token,
            )
        after = self.admission.get_lease(self.new.lease_id)
        self.assertEqual(before, after)

    def test_stale_release_is_refused_and_ownership_is_unchanged(self) -> None:
        with self.assertRaises(LeaseOwnershipError):
            self.admission.release(
                self.old.attempt_id,
                owner_id="old-owner",
                lease_token=self.old.lease_token,
                attempt_status="waiting_approval",
                task_status="waiting_approval",
                reason_code="late_release",
            )
        active = [row for row in self.lease_rows("AT-STALE") if row["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["owner_id"], "new-owner")
        self.assertEqual(ownership_violations(self.fixture.db_path), [])

    def test_stale_token_cannot_act_on_the_new_attempt(self) -> None:
        with self.assertRaises(LeaseOwnershipError):
            self.admission.heartbeat(
                self.new.attempt_id,
                owner_id="old-owner",
                lease_token=self.old.lease_token,
            )
        with self.assertRaises(LeaseOwnershipError):
            self.admission.heartbeat(
                self.new.attempt_id,
                owner_id="new-owner",
                lease_token=self.old.lease_token,
            )

    def test_new_owner_still_works(self) -> None:
        lease = self.admission.heartbeat(
            self.new.attempt_id, owner_id="new-owner", lease_token=self.new.lease_token
        )
        self.assertTrue(lease.is_active)


class ManyOwnerContentionTests(LeaseContentionTestCase):
    def test_many_owners_heartbeating_one_lease_only_the_owner_succeeds(self) -> None:
        add_rehearsal_task(self.fixture, "AT-MANY-HB")
        claim = self.admission.claim("AT-MANY-HB", owner_id="real-owner", ttl_seconds=60)
        callers = [("real-owner", claim.lease_token)] + [
            (f"impostor-{index}", claim.lease_token if index % 2 else "forged-token")
            for index in range(7)
        ]

        def beat(owner: str, token: str):
            def run() -> dict:
                try:
                    RuntimeAdmissionStore(self.fixture.db_path).heartbeat(
                        claim.attempt_id, owner_id=owner, lease_token=token
                    )
                except LeaseOwnershipError as exc:
                    return {"outcome": "refused", "error_type": type(exc).__name__}
                return {"outcome": "claimed", "owner": owner}

            return run

        outcomes = run_thread_race([beat(owner, token) for owner, token in callers])
        winners = [item for item in outcomes if item["outcome"] == "claimed"]
        self.assertEqual(winners, [{"outcome": "claimed", "owner": "real-owner"}])
        self.assertEqual(self.admission.get_lease(claim.lease_id).owner_id, "real-owner")

    def test_many_owners_reclaiming_after_a_reap_have_one_winner(self) -> None:
        add_rehearsal_task(self.fixture, "AT-MANY-RECLAIM")
        old = self.admission.claim("AT-MANY-RECLAIM", owner_id="crashed", ttl_seconds=60)
        self.expire(old.lease_id)
        reap_stale_runtime(self.fixture.db_path)
        self.recover("AT-MANY-RECLAIM", old.attempt_id)
        outcomes = run_thread_race(
            [
                (lambda index=index: explicit_claim(
                    self.fixture.db_path,
                    "AT-MANY-RECLAIM",
                    owner_id=f"contender-{index}",
                    runtime_admission=True,
                ))
                for index in range(8)
            ]
        )
        winners = [item for item in outcomes if item["outcome"] == "claimed"]
        self.assertEqual(len(winners), 1, outcomes)
        active = [row for row in self.lease_rows("AT-MANY-RECLAIM") if row["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["owner_id"], winners[0]["owner_id"])
        self.assertEqual(ownership_violations(self.fixture.db_path), [])


def explicit_claim_via_runtime(db_path: Path, task_key: str, owner_id: str):
    """Claim through the installed runtime admission store (adopts a reset)."""
    import agent_taskflow.canonical_runtime_path as canonical_path

    return canonical_path.CanonicalRuntimeAdmissionStore(db_path).claim(
        task_key, owner_id=owner_id, ttl_seconds=60
    )


if __name__ == "__main__":
    unittest.main()
