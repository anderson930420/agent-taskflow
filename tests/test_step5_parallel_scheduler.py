"""V1 Step 5 acceptance: the parallel scheduler loop (SPEC §20, §21, §43.10)."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import (  # noqa: E402
    REPO_ROOT,
    git_worktrees,
    make_fixture,
    worker_env,
    worker_launcher,
)

import agent_taskflow.parallel_scheduler as parallel_scheduler  # noqa: E402
from agent_taskflow.parallel_scheduler import run_scheduler_tick  # noqa: E402
from agent_taskflow.runtime_admission import RuntimeAdmissionStore  # noqa: E402
from agent_taskflow.runtime_capacity import set_disposable_fixture_capacity  # noqa: E402
from agent_taskflow.ticket_worktree_schema import TICKET_WORKTREE_MIGRATION_SCRIPT  # noqa: E402

TICK_CLI = "scripts/run_parallel_scheduler_tick.py"


class SchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.sync = self.fx.root / "sync"
        self.sync.mkdir()

    def tick(self, *, mode: str = "pass", expect: int = 1, wait: bool = True):
        result = run_scheduler_tick(
            self.fx.db_path,
            launcher=worker_launcher(self.sync, mode=mode, expect=expect),
            wait=wait,
            claim_timeout_seconds=60,
        )
        self.addCleanup(self._reap_workers, result)
        return result

    def _reap_workers(self, result) -> None:
        (self.sync / "release").touch()
        for process in result.workers:
            try:
                process.communicate(timeout=120)  # also closes the pipes
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            except ValueError:
                pass  # already communicated: pipes closed

    def started_records(self) -> dict[str, dict]:
        return {
            path.name.removesuffix(".started.json"): json.loads(path.read_text(encoding="utf-8"))
            for path in self.sync.glob("*.started.json")
        }

    def tickets(self, count: int, priority: str = "normal") -> list[str]:
        return [self.fx.create_ticket(f"Ticket {i}", priority=priority).task_key for i in range(count)]


class CapacityTests(SchedulerTestCase):
    """§43.10: at most max_concurrent_tasks Tickets start."""

    def test_limit_one_starts_exactly_one_highest_priority_ticket(self) -> None:
        low = self.fx.create_ticket("low", priority="low").task_key
        high = self.fx.create_ticket("high", priority="high").task_key
        normal = self.fx.create_ticket("normal").task_key

        result = self.tick()

        self.assertEqual(result.max_concurrent_tasks, 1)
        self.assertEqual([s.task_key for s in result.started], [high])
        # V1 FOLLOWUPS F8: a Ticket's success terminal.
        self.assertEqual(self.fx.status(high), "ready_for_integration")
        for untouched in (low, normal):
            self.assertEqual(self.fx.status(untouched), "created")
            self.assertEqual(self.fx.attempts(untouched), [])
        self.assertEqual(result.worker_results[0]["status"], "ready_for_integration")

    def test_limit_two_with_three_eligible_starts_two(self) -> None:
        set_disposable_fixture_capacity(self.fx.db_path, 2, fixture="step5-capacity-two")
        keys = self.tickets(3)

        result = self.tick(mode="hold", wait=False)

        self.assertEqual(result.max_concurrent_tasks, 2)
        self.assertEqual([s.task_key for s in result.started], keys[:2])
        self.assertEqual(len(self.fx.leases(active_only=True)), 2)
        self.assertEqual(self.fx.attempts(keys[2]), [])
        self.assertEqual(self.fx.status(keys[2]), "created")

    def test_capacity_already_full_starts_nothing(self) -> None:
        busy = self.fx.create_ticket("already running elsewhere").task_key
        RuntimeAdmissionStore(self.fx.db_path).claim(busy, owner_id="elsewhere", ttl_seconds=600)
        waiting = self.fx.create_ticket("waits").task_key

        result = self.tick()

        self.assertEqual(result.started, ())
        self.assertEqual(self.fx.status(waiting), "created")
        self.assertEqual(self.fx.attempts(waiting), [])


class ParallelImplementationTests(SchedulerTestCase):
    """§21: N Tickets run concurrently, each in its own worktree, one owner per Attempt."""

    def test_three_tickets_run_at_once_in_their_own_worktrees(self) -> None:
        set_disposable_fixture_capacity(self.fx.db_path, 3, fixture="step5-parallel")
        keys = self.tickets(3)
        tickets = {key: self.fx.task_row(key) for key in keys}

        result = self.tick(mode="pass", expect=3)

        self.assertEqual(sorted(s.task_key for s in result.started), sorted(keys))
        records = self.started_records()
        self.assertEqual(sorted(records), sorted(keys))
        # The barrier only opens once all three executors are running: each saw the others.
        for key in keys:
            finished = json.loads((self.sync / f"{key}.finished.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(finished["overlapping"]), sorted(keys))
            self.assertEqual(Path(records[key]["worktree_path"]), Path(tickets[key]["worktree_path"]))
        self.assertEqual(len({r["pid"] for r in records.values()}), 3)
        self.assertEqual(len({r["worktree_path"] for r in records.values()}), 3)
        self.assertEqual(
            sorted(git_worktrees(self.fx.repo)),
            sorted((Path(t["worktree_path"]), f"refs/heads/{t['branch']}") for t in tickets.values()),
        )
        for key in keys:
            self.assertEqual(self.fx.status(key), "ready_for_integration")
            attempts = self.fx.attempts(key)
            self.assertEqual(len(attempts), 1)
            leases = self.fx.leases(key)
            self.assertEqual(len(leases), 1)
            self.assertEqual(leases[0]["attempt_id"], attempts[0]["attempt_id"])
            self.assertEqual(records[key]["attempt_id"], attempts[0]["attempt_id"])
        owners = [lease["owner_id"] for lease in self.fx.leases()]
        self.assertEqual(len(set(owners)), 3)


class IdempotentLoopTests(SchedulerTestCase):
    def test_two_ticks_with_no_capacity_change_start_nothing_twice(self) -> None:
        set_disposable_fixture_capacity(self.fx.db_path, 2, fixture="step5-idempotent")
        keys = self.tickets(3)

        first = self.tick(mode="hold", wait=False)
        second = self.tick(mode="hold", wait=False)

        self.assertEqual([s.task_key for s in first.started], keys[:2])
        self.assertEqual(second.started, ())
        (self.sync / "release").touch()
        self._reap_workers(first)
        for key in keys[:2]:
            self.assertEqual(len(self.fx.attempts(key)), 1)
        self.assertEqual(self.fx.attempts(keys[2]), [])

    def test_a_tick_calls_the_reaper_first(self) -> None:
        calls: list[str] = []
        real = parallel_scheduler.reap_stale_runtime

        def spy(db_path):
            calls.append("reap")
            return real(db_path)

        with mock.patch.object(parallel_scheduler, "reap_stale_runtime", spy), mock.patch.object(
            parallel_scheduler,
            "maintain_dependencies",
            side_effect=lambda *a, **k: calls.append("dependencies")
            or parallel_scheduler.DependencyMaintenanceResult(),
        ):
            self.tick()
        self.assertEqual(calls[:2], ["reap", "dependencies"])

    def test_the_reaper_frees_an_expired_slot_before_picking(self) -> None:
        crashed = self.fx.create_ticket("crashed runtime").task_key
        claim = RuntimeAdmissionStore(self.fx.db_path).claim(crashed, owner_id="dead", ttl_seconds=60)
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?",
                (claim.lease_id,),
            )
        waiting = self.fx.create_ticket("waits for the slot").task_key

        result = self.tick()

        self.assertEqual(result.reap["expired_attempt_ids"], [claim.attempt_id])
        self.assertEqual(self.fx.status(crashed), "failed")
        self.assertEqual([s.task_key for s in result.started], [waiting])

    def test_a_tick_with_no_eligible_ticket_is_a_no_op(self) -> None:
        blocked = self.fx.create_ticket("blocked").task_key
        self.fx.set_status(blocked, "paused")
        # The Step 4 reaper's lazy lifecycle migration backfills a missing
        # task_id (F1 handoff §4.3, ruling 14); apply it before the snapshot.
        from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle

        migrate_task_attempt_lifecycle(self.fx.db_path)
        before = (self.fx.task_row(blocked), self.fx.events(blocked))

        result = self.tick()

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.started, ())
        self.assertEqual(result.workers, [])
        self.assertEqual((self.fx.task_row(blocked), self.fx.events(blocked)), before)
        self.assertEqual(git_worktrees(self.fx.repo), [])


class PreparationTests(SchedulerTestCase):
    def test_worktree_failure_ends_failed_and_the_next_ticket_starts(self) -> None:
        bad = self.fx.create_ticket("bad path", priority="high")
        good = self.fx.create_ticket("good path").task_key
        bad.worktree_path.mkdir(parents=True)

        result = self.tick()

        self.assertEqual([key for key, _ in result.preparation_failed], [bad.task_key])
        self.assertEqual(self.fx.status(bad.task_key), "failed")
        self.assertEqual(self.fx.attempts(bad.task_key), [])
        self.assertEqual([s.task_key for s in result.started], [good])

    def test_dependency_maintenance_runs_before_picking(self) -> None:
        from agent_taskflow.ticket_dependencies import set_blocked_by

        blocker = self.fx.create_ticket("blocker").task_key
        dependent = self.fx.create_ticket("dependent", priority="critical").task_key
        set_blocked_by(self.fx.db_path, dependent, blocker, actor="step5-test")
        self.fx.set_status(blocker, "cleaned")

        result = self.tick()

        self.assertEqual(result.dependencies["released"], [dependent])
        self.assertEqual([s.task_key for s in result.started], [dependent])


class TickCliTests(SchedulerTestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, TICK_CLI, *args],
            cwd=REPO_ROOT,
            env=worker_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

    def test_cli_requires_an_explicit_db_path(self) -> None:
        completed = self.run_cli()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--db-path", completed.stderr)

    def test_cli_fails_closed_without_the_step5_migration(self) -> None:
        fresh = make_fixture(migrate_step5=False)
        self.addCleanup(fresh.cleanup)
        completed = self.run_cli("--db-path", str(fresh.db_path))
        self.assertEqual(completed.returncode, 2)
        self.assertIn(TICKET_WORKTREE_MIGRATION_SCRIPT, completed.stderr)

    def test_cli_no_op_tick_reports_json(self) -> None:
        completed = self.run_cli("--db-path", str(self.fx.db_path))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["started"], [])
        self.assertFalse(payload["daemon"])
        self.assertEqual(payload["max_concurrent_tasks"], 1)


class ReviewFixSchedulerTests(SchedulerTestCase):
    """S4 and S5 from the independent review."""

    def test_a_slow_claimer_is_left_running_not_killed(self) -> None:
        key = self.tickets(1)[0]
        result = run_scheduler_tick(
            self.fx.db_path,
            launcher=worker_launcher(self.sync, mode="slow", delay=2.0),
            wait=True,
            claim_timeout_seconds=0.3,
        )
        self.addCleanup(self._reap_workers, result)
        self.assertEqual(result.started, ())
        self.assertEqual(len(result.not_started), 1)
        self.assertIn("worker left running", result.not_started[0][1])
        # wait=True waited for it; it claimed on its own and finished normally.
        self.assertEqual(result.worker_results[0]["status"], "ready_for_integration")
        self.assertEqual(self.fx.status(key), "ready_for_integration")
        self.assertEqual(self.fx.leases(key, active_only=True), [])

    def test_one_raising_candidate_does_not_abort_the_tick(self) -> None:
        first = self.fx.create_ticket("raises", priority="high").task_key
        second = self.fx.create_ticket("runs").task_key
        real = parallel_scheduler.ensure_ticket_worktree

        def flaky(db_path, task_key, **kwargs):
            if task_key == first:
                raise OSError("disk full")
            return real(db_path, task_key, **kwargs)

        with mock.patch.object(parallel_scheduler, "ensure_ticket_worktree", flaky):
            result = self.tick()
        self.assertEqual([key for key, _ in result.not_started], [first])
        self.assertIn("disk full", result.not_started[0][1])
        self.assertEqual([s.task_key for s in result.started], [second])
        self.assertEqual(self.fx.status(first), "created")


if __name__ == "__main__":
    unittest.main()
