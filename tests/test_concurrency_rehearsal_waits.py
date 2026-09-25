"""§19.3 crash-recovery waits are condition-based (FOLLOWUPS F6).

The rehearsal used to sleep a fixed time before the kill and again before the
reap, so a slow runner could kill the holder with only a sliver of lease left
or read the lease before it expired. These tests pin the replacement: each
wait polls its real condition, a slow condition only makes the rehearsal
slower, and a condition that never holds fails with a diagnostic naming it.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import agent_taskflow.concurrency_rehearsal as rehearsal
from agent_taskflow.concurrency_gate import REQUIRED_CONCURRENCY_CHECKS
from agent_taskflow.concurrency_rehearsal import (
    CRASH_LEASE_TTL_SECONDS,
    RehearsalWaitTimeout,
    kill_point_state,
    rehearse_crash_recovery,
    wait_for_condition,
)
from agent_taskflow.runtime_reaper import reap_stale_runtime
from agent_taskflow.store import connect

# The pre-F6 rehearsal TTL, for the pure kill-point tests.
TTL = 2
ACQUIRED_AT = "2026-09-25T00:00:00Z"
ACQUIRED = datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp()


def _utc(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _lease(expires: float, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(is_active=active, expires_at=_utc(expires))


def _extend_lease(db_path: Path, seconds: int) -> None:
    """Push every active lease's expiry out, as a late heartbeat would."""
    with closing(connect(db_path)) as conn, conn:
        for lease_id, expires_at in conn.execute(
            "SELECT lease_id, expires_at FROM runtime_leases WHERE is_active = 1"
        ).fetchall():
            later = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) + timedelta(
                seconds=seconds
            )
            conn.execute(
                "UPDATE runtime_leases SET expires_at = ? WHERE lease_id = ?",
                (_utc(later.timestamp()), lease_id),
            )


def _reaper_delaying_expiry(seconds: int):
    """Reaper whose first (early) call first delays the dead lease's expiry."""
    calls = []

    def reap(db_path):
        if not calls:
            _extend_lease(Path(db_path), seconds)
        calls.append(db_path)
        return reap_stale_runtime(db_path)

    return reap


_real_wait_for_lease_expiry = rehearsal.wait_for_lease_expiry


def _short_expiry_wait(db_path, lease_id, *, timeout):
    """The real expiry wait with a 1s deadline; the kill wait keeps its own."""
    return _real_wait_for_lease_expiry(db_path, lease_id, timeout=1.0)


class WaitForConditionTests(unittest.TestCase):
    def test_returns_the_observation_once_the_condition_holds(self) -> None:
        polls = iter(range(10))

        def probe():
            count = next(polls)
            return count >= 3, count

        self.assertEqual(wait_for_condition("count reaches 3", probe, poll_interval=0), 3)

    def test_a_condition_that_never_holds_fails_naming_the_condition(self) -> None:
        with self.assertRaises(RehearsalWaitTimeout) as caught:
            wait_for_condition(
                "the impossible condition",
                lambda: (False, "still false"),
                timeout=0.2,
                poll_interval=0.01,
            )
        message = str(caught.exception)
        self.assertIn("timed out after", message)
        self.assertIn("the impossible condition", message)
        self.assertIn("still false", message)


class KillPointStateTests(unittest.TestCase):
    def state(self, lease, now: float) -> str:
        return kill_point_state(
            lease, acquired_at=ACQUIRED_AT, lease_ttl_seconds=TTL, now=now
        )

    def test_waits_until_the_lease_outlives_its_first_ttl(self) -> None:
        self.assertEqual(self.state(_lease(ACQUIRED + TTL), ACQUIRED + 1.5), "wait")

    def test_the_old_fixed_sleep_kill_point_is_refused_with_a_sliver_left(self) -> None:
        # The old code killed at acquired + TTL + 1 whatever was left. Leases
        # are stamped in whole seconds, so 0.4s left was possible, which the
        # early reap could lose to a loaded runner.
        now = ACQUIRED + TTL + 1.6
        self.assertEqual(self.state(_lease(ACQUIRED + TTL + 2), now), "wait")

    def test_ready_once_past_the_ttl_with_enough_left(self) -> None:
        now = ACQUIRED + TTL + 1.1
        self.assertEqual(self.state(_lease(ACQUIRED + TTL + 3), now), "ready")

    def test_a_lapsed_lease_ends_the_wait_so_the_checks_report_it(self) -> None:
        now = ACQUIRED + TTL + 1
        self.assertEqual(self.state(None, now), "lapsed")
        self.assertEqual(self.state(_lease(now + TTL, active=False), now), "lapsed")
        self.assertEqual(self.state(_lease(now - 0.5), now), "lapsed")


class CrashRecoveryWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_lease_expiry_later_than_the_old_fixed_sleep_still_passes(self) -> None:
        # The old code slept TTL + 0.5s after the kill before reading the
        # lease. Delay the dead lease's expiry by another full TTL past that.
        ttl = CRASH_LEASE_TTL_SECONDS
        with mock.patch.object(
            rehearsal, "reap_stale_runtime", _reaper_delaying_expiry(ttl)
        ):
            result = rehearse_crash_recovery(self.root / "crash")
        for name in REQUIRED_CONCURRENCY_CHECKS["19.3"]:
            with self.subTest(check=name):
                self.assertIs(result["checks"][name], True, result["details"])
        waits = result["details"]["waits"]
        self.assertGreater(waits["lease_expiry_seconds"], ttl + 0.5)
        self.assertGreater(waits["lease_seconds_left_at_kill"], 0)
        self.assertGreater(waits["early_reap_seconds_before_expiry"], 0)

    def test_a_lease_that_never_expires_fails_with_the_timeout_diagnostic(self) -> None:
        with mock.patch.multiple(
            rehearsal,
            reap_stale_runtime=_reaper_delaying_expiry(3600),
            wait_for_lease_expiry=_short_expiry_wait,
        ):
            with self.assertRaises(RehearsalWaitTimeout) as caught:
                rehearse_crash_recovery(self.root / "crash")
        self.assertIn("to pass its expires_at", str(caught.exception))

    def test_a_kill_point_that_never_comes_fails_with_the_timeout_diagnostic(self) -> None:
        with mock.patch.object(rehearsal, "KILL_MIN_LEASE_LEFT_FRACTION", 10.0):
            with self.assertRaises(RehearsalWaitTimeout) as caught:
                rehearse_crash_recovery(self.root / "crash", wait_timeout_seconds=1.0)
        self.assertIn("through the holder's heartbeat", str(caught.exception))

    def test_a_timed_out_wait_fails_every_19_3_check_in_the_evidence(self) -> None:
        passing = {
            section: {
                "checks": {name: True for name in REQUIRED_CONCURRENCY_CHECKS[section]},
                "details": {},
                "databases": [],
            }
            for section in ("19.1", "19.2")
        }
        with mock.patch.multiple(
            rehearsal,
            rehearse_atomic_claim=lambda *a, **k: passing["19.1"],
            rehearse_concurrent_writes=lambda *a, **k: passing["19.2"],
            reap_stale_runtime=_reaper_delaying_expiry(3600),
            wait_for_lease_expiry=_short_expiry_wait,
        ):
            evidence = rehearsal.run_concurrency_rehearsal(output_dir=self.root / "out")
        self.assertFalse(evidence["all_checks_passed"])
        for name in REQUIRED_CONCURRENCY_CHECKS["19.3"]:
            with self.subTest(check=name):
                self.assertIs(evidence["checks"][name], False)
        error = evidence["details"]["19.3"]["error"]
        self.assertTrue(error.startswith("RehearsalWaitTimeout: timed out after"), error)
        self.assertIn("to pass its expires_at", error)


if __name__ == "__main__":
    unittest.main()
