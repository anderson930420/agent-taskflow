"""Capacity limit and concurrency gate (V1 Step 4, SPEC §19.4, §20, §43.9-10).

``max_concurrent_tasks`` is a global runtime control, default 1 on every
database (ruling 15), stored with the existing runtime controls. It is enforced
inside the claim transaction and can rise above 1 only against passing Step 4
rehearsal evidence.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow.concurrency_gate import (
    CONCURRENCY_REHEARSAL_SCHEMA_VERSION,
    REQUIRED_CONCURRENCY_CHECKS,
    evaluate_concurrency_evidence,
)
from agent_taskflow.concurrency_rehearsal import (
    add_rehearsal_task,
    create_rehearsal_fixture,
    explicit_claim,
    run_thread_race,
)
from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.reset_lineage import ResetLineageStore
from agent_taskflow.runtime_admission import (
    RuntimeAdmissionError,
    RuntimeAdmissionStore,
    RuntimeCapacityExceededError,
)
from agent_taskflow.runtime_capacity import (
    DEFAULT_MAX_CONCURRENT_TASKS,
    ConcurrencyGateRefused,
    RuntimeCapacityError,
    list_runtime_capacity_events,
    read_runtime_capacity,
    set_disposable_fixture_capacity,
    set_max_concurrent_tasks,
)
from agent_taskflow.runtime_capacity_schema import (
    DISPOSABLE_FIXTURE_CAPACITY_REASON,
    RUNTIME_CAPACITY_MIGRATION,
    migrate_runtime_capacity,
)
from agent_taskflow.store import connect

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_head(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def write_evidence(path: Path, *, repo_sha: str, overrides: dict | None = None) -> Path:
    checks = {
        name: True
        for section in REQUIRED_CONCURRENCY_CHECKS.values()
        for name in section
    }
    payload = {
        "schema_version": CONCURRENCY_REHEARSAL_SCHEMA_VERSION,
        "repo_sha": repo_sha,
        "generated_at": "2026-09-11T00:00:00Z",
        "disposable_database": True,
        "production_database_touched": False,
        "checks": checks,
        "all_checks_passed": True,
    }
    for key, value in (overrides or {}).items():
        if key == "checks":
            payload["checks"].update(value)
        else:
            payload[key] = value
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


class CapacityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = create_rehearsal_fixture(self.root / "fixture")
        self.db = self.fixture.db_path
        self.repo_sha = _git_head(REPO_ROOT)

    def deploy(self) -> None:
        migrate_runtime_capacity(self.db)

    def passing_evidence(self, name: str = "evidence.json") -> Path:
        return write_evidence(self.root / name, repo_sha=self.repo_sha)

    def active_leases(self) -> int:
        with closing(connect(self.db)) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM runtime_leases WHERE is_active = 1"
            ).fetchone()[0]


class DefaultLimitTests(CapacityTestCase):
    def test_default_is_one(self) -> None:
        self.assertEqual(DEFAULT_MAX_CONCURRENT_TASKS, 1)
        setting = read_runtime_capacity(self.db)
        self.assertEqual(setting.max_concurrent_tasks, 1)
        self.assertEqual(setting.source, "default")

    def test_default_reads_as_one_through_the_runtime_control_store(self) -> None:
        setting = RuntimeControlStore(self.db).runtime_capacity()
        self.assertEqual(setting.max_concurrent_tasks, 1)

    def test_with_limit_one_a_second_concurrent_claim_is_refused(self) -> None:
        add_rehearsal_task(self.fixture, "AT-CAP-A")
        add_rehearsal_task(self.fixture, "AT-CAP-B")
        admission = RuntimeAdmissionStore(self.db)
        admission.claim("AT-CAP-A", owner_id="first")
        with self.assertRaises(RuntimeCapacityExceededError) as raised:
            admission.claim("AT-CAP-B", owner_id="second")
        error = raised.exception
        self.assertIsInstance(error, RuntimeAdmissionError)
        self.assertEqual(error.reason_code, "runtime_capacity_exceeded")
        self.assertEqual(error.max_concurrent_tasks, 1)
        self.assertEqual(error.active_executor_leases, 1)
        self.assertEqual(self.active_leases(), 1)
        with closing(connect(self.db)) as conn:
            b = conn.execute(
                "SELECT status, active_attempt_id FROM tasks WHERE task_key = 'AT-CAP-B'"
            ).fetchone()
            b_attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts JOIN tasks USING(task_id) "
                "WHERE tasks.task_key = 'AT-CAP-B'"
            ).fetchone()[0]
        self.assertEqual(b["status"], "queued")
        self.assertIsNone(b["active_attempt_id"])
        self.assertEqual(b_attempts, 0)

    def test_with_limit_one_concurrent_claimers_on_different_tickets_get_one_slot(self) -> None:
        keys = [f"AT-CAP-RACE-{index}" for index in range(8)]
        for key in keys:
            add_rehearsal_task(self.fixture, key)
        outcomes = run_thread_race(
            [
                (lambda key=key: explicit_claim(self.db, key, owner_id=f"owner-{key}"))
                for key in keys
            ]
        )
        winners = [item for item in outcomes if item["outcome"] == "claimed"]
        self.assertEqual(len(winners), 1, outcomes)
        refusals = {item["error_type"] for item in outcomes if item["outcome"] != "claimed"}
        self.assertEqual(refusals, {"RuntimeCapacityExceededError"})
        self.assertEqual(self.active_leases(), 1)

    def test_a_released_slot_can_be_claimed_again(self) -> None:
        add_rehearsal_task(self.fixture, "AT-CAP-R1")
        add_rehearsal_task(self.fixture, "AT-CAP-R2")
        admission = RuntimeAdmissionStore(self.db)
        first = admission.claim("AT-CAP-R1", owner_id="first")
        admission.release(
            first.attempt_id,
            owner_id="first",
            lease_token=first.lease_token,
            attempt_status="waiting_approval",
            task_status="waiting_approval",
            reason_code="done",
        )
        second = admission.claim("AT-CAP-R2", owner_id="second")
        self.assertEqual(self.active_leases(), 1)
        self.assertEqual(second.task_key, "AT-CAP-R2")

    def test_expired_but_unreaped_leases_still_hold_their_slot(self) -> None:
        add_rehearsal_task(self.fixture, "AT-CAP-E1")
        add_rehearsal_task(self.fixture, "AT-CAP-E2")
        admission = RuntimeAdmissionStore(self.db)
        stale = admission.claim("AT-CAP-E1", owner_id="crashed")
        with closing(connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE lease_id = ?",
                (stale.lease_id,),
            )
        with self.assertRaises(RuntimeCapacityExceededError):
            admission.claim("AT-CAP-E2", owner_id="next")
        admission.expire_stale_leases()
        admission.claim("AT-CAP-E2", owner_id="next")

    def test_capacity_applies_to_the_reset_retry_claim_path(self) -> None:
        import agent_taskflow.canonical_runtime_path as canonical_path

        add_rehearsal_task(self.fixture, "AT-CAP-RETRY")
        add_rehearsal_task(self.fixture, "AT-CAP-HOLDER")
        runtime = canonical_path.CanonicalRuntimeAdmissionStore(self.db)
        old = runtime.claim("AT-CAP-RETRY", owner_id="crashed", ttl_seconds=60)
        with closing(connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE lease_id = ?",
                (old.lease_id,),
            )
        runtime.expire_stale_leases()
        ResetLineageStore(self.db).reserve_retry(
            "AT-CAP-RETRY", reason="capacity rehearsal", actor="test"
        )
        runtime.claim("AT-CAP-HOLDER", owner_id="holder")
        with self.assertRaises(RuntimeCapacityExceededError):
            runtime.claim("AT-CAP-RETRY", owner_id="retry")
        with closing(connect(self.db)) as conn:
            lineage_state = conn.execute(
                "SELECT state FROM reset_lineages ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(lineage_state, "reserved")
        self.assertEqual(self.active_leases(), 1)


class EveryDatabaseIsBoundedTests(CapacityTestCase):
    """Ruling 15: the default of 1 applies to every database, no migration."""

    def assert_no_capacity_table(self) -> None:
        with closing(connect(self.db)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'runtime_capacity_controls'"
            ).fetchone()
        self.assertIsNone(table)

    def test_a_database_that_never_stored_a_value_is_limited_to_one(self) -> None:
        add_rehearsal_task(self.fixture, "AT-NOVALUE-A")
        add_rehearsal_task(self.fixture, "AT-NOVALUE-B")
        admission = RuntimeAdmissionStore(self.db)
        admission.claim("AT-NOVALUE-A", owner_id="a")
        with self.assertRaises(RuntimeCapacityExceededError):
            admission.claim("AT-NOVALUE-B", owner_id="b")
        self.assertEqual(self.active_leases(), 1)
        self.assert_no_capacity_table()

    def test_reading_and_refused_claims_never_install_the_tables(self) -> None:
        self.assertEqual(read_runtime_capacity(self.db).max_concurrent_tasks, 1)
        self.assert_no_capacity_table()

    def test_installed_tables_without_a_row_still_mean_one(self) -> None:
        self.deploy()
        self.assertEqual(read_runtime_capacity(self.db).source, "default")
        add_rehearsal_task(self.fixture, "AT-EMPTY-A")
        add_rehearsal_task(self.fixture, "AT-EMPTY-B")
        RuntimeAdmissionStore(self.db).claim("AT-EMPTY-A", owner_id="a")
        with self.assertRaises(RuntimeCapacityExceededError):
            RuntimeAdmissionStore(self.db).claim("AT-EMPTY-B", owner_id="b")


class DisposableFixtureCapacityTests(CapacityTestCase):
    """Ruling 15b: fixtures that hold several claims set their own value."""

    def test_fixture_value_is_enforced_and_labelled(self) -> None:
        setting = set_disposable_fixture_capacity(self.db, 2, fixture="unit-test")
        self.assertEqual(setting.max_concurrent_tasks, 2)
        self.assertEqual(setting.source, "disposable_fixture")
        self.assertEqual(setting.requested_by, "fixture:unit-test")
        self.assertIsNone(setting.evidence_sha256)
        for key in ("AT-FIX-A", "AT-FIX-B", "AT-FIX-C"):
            add_rehearsal_task(self.fixture, key)
        admission = RuntimeAdmissionStore(self.db)
        admission.claim("AT-FIX-A", owner_id="a")
        admission.claim("AT-FIX-B", owner_id="b")
        with self.assertRaises(RuntimeCapacityExceededError) as raised:
            admission.claim("AT-FIX-C", owner_id="c")
        self.assertEqual(raised.exception.max_concurrent_tasks, 2)

    def test_fixture_value_is_audited_with_its_own_reason(self) -> None:
        set_disposable_fixture_capacity(self.db, 3, fixture="unit-test")
        events = list_runtime_capacity_events(self.db)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason_code"], DISPOSABLE_FIXTURE_CAPACITY_REASON)
        self.assertEqual(events[0]["to_max_concurrent_tasks"], 3)
        self.assertIsNone(events[0]["evidence_sha256"])

    def test_fixture_setter_refuses_the_default_state_database(self) -> None:
        with mock.patch(
            "agent_taskflow.runtime_capacity.default_db_path", return_value=self.db
        ):
            with self.assertRaises(RuntimeCapacityError):
                set_disposable_fixture_capacity(self.db, 2, fixture="unit-test")
        self.assertEqual(read_runtime_capacity(self.db).source, "default")

    def test_fixture_setter_is_not_on_the_operator_cli(self) -> None:
        source = (REPO_ROOT / "scripts" / "runtime_control.py").read_text(encoding="utf-8")
        self.assertNotIn("set_disposable_fixture_capacity", source)
        self.assertNotIn("disposable_fixture", source)

    def test_the_operator_path_still_needs_evidence_after_a_fixture_value(self) -> None:
        set_disposable_fixture_capacity(self.db, 3, fixture="unit-test")
        with self.assertRaises(ConcurrencyGateRefused):
            set_max_concurrent_tasks(self.db, 3, actor="operator")


class GateTests(CapacityTestCase):
    def test_raising_above_one_without_evidence_is_refused(self) -> None:
        with self.assertRaises(ConcurrencyGateRefused) as raised:
            set_max_concurrent_tasks(self.db, 2, actor="operator")
        self.assertIn("evidence", str(raised.exception))
        self.assertEqual(read_runtime_capacity(self.db).max_concurrent_tasks, 1)

    def test_refusal_writes_nothing(self) -> None:
        with self.assertRaises(ConcurrencyGateRefused):
            set_max_concurrent_tasks(self.db, 3, actor="operator")
        self.assertEqual(read_runtime_capacity(self.db).source, "default")
        self.assertEqual(list_runtime_capacity_events(self.db), [])
        with closing(connect(self.db)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'runtime_capacity_controls'"
            ).fetchone()
        self.assertIsNone(table)

    def test_raising_above_one_with_failing_evidence_is_refused(self) -> None:
        cases = {
            "missing check": {"checks": {"crash_lease_expired": False}},
            "wrong schema": {"schema_version": "m1_exit_gate_audit.v1"},
            "other repo sha": {"repo_sha": "0" * 40},
            "touched production": {"production_database_touched": True},
            "not disposable": {"disposable_database": False},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                evidence = write_evidence(
                    self.root / f"{label.replace(' ', '-')}.json",
                    repo_sha=self.repo_sha,
                    overrides=overrides,
                )
                with self.assertRaises(ConcurrencyGateRefused) as raised:
                    set_max_concurrent_tasks(
                        self.db, 2, actor="operator", evidence_path=evidence
                    )
                self.assertEqual(raised.exception.report["gate"], "blocked")
        self.assertEqual(read_runtime_capacity(self.db).max_concurrent_tasks, 1)

    def test_missing_or_malformed_evidence_file_is_refused(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        for path in (self.root / "absent.json", bad):
            with self.subTest(path=path.name):
                report = evaluate_concurrency_evidence(path, repo_root=REPO_ROOT)
                self.assertEqual(report["gate"], "blocked")
                with self.assertRaises(ConcurrencyGateRefused):
                    set_max_concurrent_tasks(
                        self.db, 2, actor="operator", evidence_path=path
                    )

    def test_gate_is_read_only(self) -> None:
        evidence = self.passing_evidence()
        before = evidence.read_bytes()
        report = evaluate_concurrency_evidence(evidence, repo_root=REPO_ROOT)
        self.assertEqual(report["gate"], "passed", report)
        self.assertTrue(report["read_only"])
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(
            report["evidence_sha256"], hashlib.sha256(before).hexdigest()
        )

    def test_with_passing_evidence_and_limit_k_at_most_k_claims_are_active(self) -> None:
        k = 3
        evidence = self.passing_evidence()
        setting = set_max_concurrent_tasks(
            self.db, k, actor="operator", evidence_path=evidence
        )
        self.assertEqual(setting.max_concurrent_tasks, k)
        self.assertEqual(setting.source, "configured")
        self.assertEqual(
            setting.evidence_sha256, hashlib.sha256(evidence.read_bytes()).hexdigest()
        )
        keys = [f"AT-CAP-K-{index}" for index in range(2 * k + 2)]
        for key in keys:
            add_rehearsal_task(self.fixture, key)
        outcomes = run_thread_race(
            [
                (lambda key=key: explicit_claim(self.db, key, owner_id=f"owner-{key}"))
                for key in keys
            ]
        )
        winners = [item for item in outcomes if item["outcome"] == "claimed"]
        self.assertEqual(len(winners), k, outcomes)
        refusals = {item["error_type"] for item in outcomes if item["outcome"] != "claimed"}
        self.assertEqual(refusals, {"RuntimeCapacityExceededError"})
        self.assertEqual(self.active_leases(), k)

    def test_lowering_to_one_never_needs_evidence(self) -> None:
        set_max_concurrent_tasks(
            self.db, 4, actor="operator", evidence_path=self.passing_evidence()
        )
        setting = set_max_concurrent_tasks(self.db, 1, actor="operator")
        self.assertEqual(setting.max_concurrent_tasks, 1)
        self.assertIsNone(setting.evidence_sha256)

    def test_invalid_values_are_rejected(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    set_max_concurrent_tasks(self.db, value, actor="operator")
        with self.assertRaises(ValueError):
            set_max_concurrent_tasks(self.db, 1, actor="  ")

    def test_database_refuses_a_limit_above_one_without_evidence(self) -> None:
        self.deploy()
        with closing(connect(self.db)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        """
                        INSERT INTO runtime_capacity_controls(
                            scope_kind, scope_id, max_concurrent_tasks,
                            reason_code, requested_by, requested_at, generation
                        ) VALUES ('global', '*', 5, 'forged', 'intruder',
                                  '2026-09-11T00:00:00Z', 1)
                        """
                    )

    def test_every_change_is_audited_append_only(self) -> None:
        set_max_concurrent_tasks(
            self.db, 2, actor="operator-a", evidence_path=self.passing_evidence()
        )
        set_max_concurrent_tasks(self.db, 1, actor="operator-b")
        events = list_runtime_capacity_events(self.db)
        self.assertEqual(
            [(event["from_max_concurrent_tasks"], event["to_max_concurrent_tasks"],
              event["actor"], event["generation"]) for event in events],
            [(None, 2, "operator-a", 1), (2, 1, "operator-b", 2)],
        )
        self.assertIsNotNone(events[0]["evidence_sha256"])
        with closing(connect(self.db)) as conn:
            for statement in (
                "UPDATE runtime_capacity_control_events SET actor = 'x'",
                "DELETE FROM runtime_capacity_control_events",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        with conn:
                            conn.execute(statement)

    def test_migration_is_recorded_and_idempotent(self) -> None:
        self.deploy()
        self.deploy()
        with closing(connect(self.db)) as conn:
            recorded = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (RUNTIME_CAPACITY_MIGRATION,),
            ).fetchone()[0]
        self.assertEqual(recorded, 1)


class RuntimeControlCliTests(CapacityTestCase):
    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-S", str(REPO_ROOT / "scripts" / "runtime_control.py"), *args],
            cwd=REPO_ROOT,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_capacity_status_reports_the_default(self) -> None:
        payload = json.loads(
            self.run_cli("capacity", "--db-path", str(self.db)).stdout
        )
        self.assertEqual(payload["max_concurrent_tasks"], 1)
        self.assertEqual(payload["source"], "default")
        self.assertEqual(payload["scope"], "global")

    def test_set_capacity_above_one_without_evidence_exits_two(self) -> None:
        completed = self.run_cli(
            "set-capacity",
            "--db-path",
            str(self.db),
            "--actor",
            "operator",
            "--max-concurrent-tasks",
            "2",
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["gate"]["gate"], "blocked")

    def test_set_capacity_with_passing_evidence(self) -> None:
        evidence = self.passing_evidence()
        completed = self.run_cli(
            "set-capacity",
            "--db-path",
            str(self.db),
            "--actor",
            "operator",
            "--max-concurrent-tasks",
            "3",
            "--evidence-path",
            str(evidence),
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["max_concurrent_tasks"], 3)
        self.assertEqual(payload["source"], "configured")
        status = json.loads(self.run_cli("capacity", "--db-path", str(self.db)).stdout)
        self.assertEqual(status["max_concurrent_tasks"], 3)

    def test_set_capacity_requires_an_actor(self) -> None:
        completed = self.run_cli(
            "set-capacity",
            "--db-path",
            str(self.db),
            "--max-concurrent-tasks",
            "1",
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--actor", completed.stderr)

    def test_existing_actions_are_unchanged(self) -> None:
        payload = json.loads(self.run_cli("status", "--db-path", str(self.db)).stdout)
        self.assertEqual(payload["effective_mode"], "running")
        self.assertNotIn("max_concurrent_tasks", payload)


if __name__ == "__main__":
    unittest.main()
