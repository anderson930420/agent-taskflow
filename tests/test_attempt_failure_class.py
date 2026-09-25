"""M2 Exit Gate row 2: the Attempt failure class in durable state.

Every failed Attempt records one of execution_failure, validation_failure or
tool_error (or an explicit `unknown`) on its terminal lifecycle event, and the
outcome ledger carries it. These tests go through the real dispatcher and the
installed canonical runtime store; statuses are asserted unchanged beside it.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import (  # noqa: E402
    RecordingExecutor,
    RecordingValidator,
    make_fixture,
)

from agent_taskflow import outcome_ledger  # noqa: E402
from agent_taskflow.attempt_failure_class import (  # noqa: E402
    FAILURE_CLASS_BY_KIND,
    FAILURE_CLASS_EXECUTION,
    FAILURE_CLASS_TOOL_ERROR,
    FAILURE_CLASS_UNKNOWN,
    FAILURE_CLASS_VALIDATION,
    FAILURE_CLASSES,
    REASON_KIND_NOT_SUPPLIED,
    REASON_KIND_UNMAPPED,
    RECORDED_FAILURE_CLASSES,
    failure_class_for_kind,
    failure_class_metadata,
    read_attempt_failure_class,
)
from agent_taskflow.canonical_runtime_path import canonical_runtime_task_store  # noqa: E402
from agent_taskflow.models import TaskWorktreeRecord  # noqa: E402
from agent_taskflow.runtime_admission import RuntimeAdmissionStore  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.ticket_lifecycle import (  # noqa: E402
    FAILURE_EXECUTOR,
    FAILURE_GOVERNANCE,
    FAILURE_KINDS,
    FAILURE_LEASE_EXPIRED,
    FAILURE_VALIDATOR_ERROR,
    FAILURE_VALIDATOR_RED,
    FAILURE_WORKTREE,
)
from agent_taskflow.ticket_worktree import ensure_ticket_worktree  # noqa: E402


class FailureClassMappingTests(unittest.TestCase):
    def test_the_three_exit_gate_classes_map_from_existing_kinds(self) -> None:
        self.assertEqual(
            FAILURE_CLASS_BY_KIND,
            {
                FAILURE_EXECUTOR: FAILURE_CLASS_EXECUTION,
                FAILURE_VALIDATOR_RED: FAILURE_CLASS_VALIDATION,
                FAILURE_VALIDATOR_ERROR: FAILURE_CLASS_TOOL_ERROR,
            },
        )
        self.assertEqual(
            FAILURE_CLASSES, {"execution_failure", "validation_failure", "tool_error"}
        )

    def test_every_failure_kind_maps_to_exactly_one_recorded_class(self) -> None:
        for kind in FAILURE_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(failure_class_for_kind(kind), RECORDED_FAILURE_CLASSES)

    def test_kinds_outside_the_exit_gate_classes_fail_closed_to_unknown(self) -> None:
        for kind in (FAILURE_GOVERNANCE, FAILURE_WORKTREE, FAILURE_LEASE_EXPIRED, "new-kind"):
            with self.subTest(kind=kind):
                metadata = failure_class_metadata("failed", kind)
                self.assertEqual(metadata["failure_class"], FAILURE_CLASS_UNKNOWN)
                self.assertEqual(metadata["failure_kind"], kind)
                self.assertEqual(metadata["failure_class_reason"], REASON_KIND_UNMAPPED)

    def test_missing_kind_is_recorded_as_unknown_not_guessed(self) -> None:
        metadata = failure_class_metadata("failed", None)
        self.assertEqual(metadata["failure_class"], FAILURE_CLASS_UNKNOWN)
        self.assertIsNone(metadata["failure_kind"])
        self.assertEqual(metadata["failure_class_reason"], REASON_KIND_NOT_SUPPLIED)

    def test_non_failure_releases_record_nothing(self) -> None:
        for status in ("ready_for_integration", "waiting_approval", "completed", "canceled"):
            with self.subTest(status=status):
                self.assertEqual(failure_class_metadata(status, FAILURE_EXECUTOR), {})


class DispatcherFailureClassTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.key = self.fx.create_ticket("Failure class").task_key

    def last_attempt(self, task_key: str | None = None) -> dict:
        return self.fx.attempts(task_key or self.key)[-1]

    def terminal_event(self, attempt_id: str) -> dict:
        with closing(self.fx.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM lifecycle_events WHERE attempt_id = ?"
                " ORDER BY event_id DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        event = dict(row)
        event["metadata"] = json.loads(event["metadata_json"])
        return event

    def ledger(self, task_key: str, attempt_id: str) -> dict:
        paths = [
            event["payload"]["path"]
            for event in self.fx.events(task_key)
            if event["source"] == "outcome_ledger"
            and event["payload"].get("attempt_id") == attempt_id
            and event["payload"].get("path")
        ]
        self.assertEqual(len(paths), 1, paths)
        return json.loads(Path(paths[0]).read_text(encoding="utf-8"))

    def assert_class(
        self,
        failure_class: str,
        kind: str,
        *,
        task_key: str | None = None,
    ) -> dict:
        key = task_key or self.key
        attempt = self.last_attempt(key)
        self.assertEqual(attempt["is_active"], 0)
        recorded = read_attempt_failure_class(self.fx.db_path, attempt["attempt_id"])
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded["failure_class"], failure_class)
        self.assertEqual(recorded["failure_kind"], kind)
        self.assertEqual(recorded["to_status"], attempt["status"])
        field = self.ledger(key, attempt["attempt_id"])["fields"]["failure_class"]
        self.assertEqual(field["value"], failure_class)
        self.assertEqual(field["provenance"], "observed")
        self.assertEqual(field["failure_kind"], kind)
        self.assertEqual(
            field["source"],
            f"lifecycle_events.event_id={recorded['event_id']}.metadata_json",
        )
        return attempt


class EndToEndFailureClassTests(DispatcherFailureClassTestCase):
    def test_executor_failure_is_execution_failure(self) -> None:
        result = self.fx.dispatch(self.key, RecordingExecutor("failed", summary="build broke"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.fx.status(self.key), "failed")
        attempt = self.assert_class(FAILURE_CLASS_EXECUTION, FAILURE_EXECUTOR)
        self.assertEqual(attempt["status"], "failed")

    def test_validator_red_is_validation_failure(self) -> None:
        result = self.fx.dispatch(
            self.key, RecordingExecutor(), (RecordingValidator(status="failed"),)
        )
        self.assertEqual(result.status, "needs_decision")
        self.assertEqual(self.fx.status(self.key), "needs_decision")
        attempt = self.assert_class(FAILURE_CLASS_VALIDATION, FAILURE_VALIDATOR_RED)
        self.assertEqual(attempt["status"], "validation_failed")

    def test_validator_tool_error_is_tool_error(self) -> None:
        result = self.fx.dispatch(
            self.key,
            RecordingExecutor(),
            (RecordingValidator(raise_exc=RuntimeError("validator crashed")),),
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.fx.status(self.key), "failed")
        attempt = self.assert_class(FAILURE_CLASS_TOOL_ERROR, FAILURE_VALIDATOR_ERROR)
        self.assertEqual(attempt["status"], "failed")

    def test_validator_without_a_verdict_is_tool_error(self) -> None:
        result = self.fx.dispatch(
            self.key, RecordingExecutor(), (RecordingValidator(status="blocked"),)
        )
        self.assertEqual(result.status, "failed")
        self.assert_class(FAILURE_CLASS_TOOL_ERROR, FAILURE_VALIDATOR_ERROR)

    def test_executor_crash_is_execution_failure(self) -> None:
        result = self.fx.dispatch(
            self.key, RecordingExecutor(raise_exc=RuntimeError("executor crashed"))
        )
        self.assertEqual(result.status, "failed")
        self.assert_class(FAILURE_CLASS_EXECUTION, FAILURE_EXECUTOR)

    def test_legacy_task_keeps_blocked_and_records_the_class(self) -> None:
        self.fx.add_legacy_task("AT-LEGACY-CLASS")
        TaskMirrorStore(self.fx.db_path).upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-LEGACY-CLASS",
                repo_path=self.fx.repo,
                worktree_path=self.fx.repo / ".worktrees" / "AT-LEGACY-CLASS",
                branch="task/AT-LEGACY-CLASS",
                base_branch="main",
                status="active",
            )
        )
        result = self.fx.dispatch("AT-LEGACY-CLASS", RecordingExecutor("failed"))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.fx.status("AT-LEGACY-CLASS"), "blocked")
        self.assert_class(
            FAILURE_CLASS_EXECUTION, FAILURE_EXECUTOR, task_key="AT-LEGACY-CLASS"
        )

    def test_success_records_no_class_and_ledger_says_not_applicable(self) -> None:
        result = self.fx.dispatch(self.key, RecordingExecutor())
        self.assertEqual(result.status, "ready_for_integration")
        attempt = self.last_attempt()
        self.assertIsNone(read_attempt_failure_class(self.fx.db_path, attempt["attempt_id"]))
        self.assertNotIn(
            "failure_class", self.terminal_event(attempt["attempt_id"])["metadata"]
        )
        field = self.ledger(self.key, attempt["attempt_id"])["fields"]["failure_class"]
        self.assertIsNone(field["value"])
        self.assertEqual(field["provenance"], "not_applicable")

    def test_pre_claim_failure_writes_no_attempt_and_leaves_no_pending_kind(self) -> None:
        ticket = self.fx.create_ticket("Governance refusal")
        ticket.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        ticket.artifact_dir.write_text("not a directory\n", encoding="utf-8")
        dispatcher = self.fx.dispatcher(RecordingExecutor())
        try:
            result = dispatcher.dispatch_task(ticket.task_key)
            self.assertEqual(result.status, "failed")
            self.assertEqual(dispatcher.store._failure_kinds, {})
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.assertEqual(self.fx.attempts(ticket.task_key), [])

    def test_a_later_event_on_the_ended_attempt_does_not_hide_the_class(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        attempt = self.last_attempt()
        terminal = self.terminal_event(attempt["attempt_id"])
        with closing(self.fx.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO lifecycle_events(task_id, attempt_id, from_status,"
                " to_status, reason_code, actor, timestamp, metadata_json)"
                " VALUES (?, ?, ?, ?, 'later_note', 'test', ?, '{}')",
                (
                    attempt["task_id"],
                    attempt["attempt_id"],
                    attempt["status"],
                    attempt["status"],
                    terminal["timestamp"],
                ),
            )
        recorded = read_attempt_failure_class(self.fx.db_path, attempt["attempt_id"])
        self.assertEqual(recorded["event_id"], terminal["event_id"])
        self.assertEqual(recorded["failure_class"], FAILURE_CLASS_EXECUTION)

    def test_release_without_a_kind_is_recorded_as_unknown(self) -> None:
        # A caller outside the dispatcher (the legacy approved runner, an
        # operator path) releases a failed Attempt without naming a kind.
        self.assertTrue(ensure_ticket_worktree(self.fx.db_path, self.key).ok)
        store = canonical_runtime_task_store(self.fx.db_path)
        try:
            store.update_task_status(self.key, "preparing", source="test")
            store.update_task_status(self.key, "failed", source="test", message="gone")
        finally:
            store.shutdown_runtime_supervisors()
        attempt = self.last_attempt()
        recorded = read_attempt_failure_class(self.fx.db_path, attempt["attempt_id"])
        self.assertEqual(recorded["failure_class"], FAILURE_CLASS_UNKNOWN)
        self.assertIsNone(recorded["failure_kind"])
        self.assertEqual(recorded["failure_class_reason"], REASON_KIND_NOT_SUPPLIED)
        field = self.ledger(self.key, attempt["attempt_id"])["fields"]["failure_class"]
        self.assertEqual(field["value"], FAILURE_CLASS_UNKNOWN)
        self.assertEqual(field["reason"], REASON_KIND_NOT_SUPPLIED)


class ExecutorVersusToolErrorRegressionTests(DispatcherFailureClassTestCase):
    """The audit's finding: these two looked identical in durable state."""

    def test_executor_crash_and_validator_crash_differ_in_durable_state(self) -> None:
        other = self.fx.create_ticket("Validator crash").task_key
        self.fx.dispatch(self.key, RecordingExecutor(raise_exc=RuntimeError("boom")))
        self.fx.dispatch(
            other,
            RecordingExecutor(),
            (RecordingValidator(raise_exc=RuntimeError("boom")),),
        )

        executor_attempt = self.last_attempt(self.key)
        tool_attempt = self.last_attempt(other)
        # Status columns are deliberately unchanged, and still identical ...
        columns = ("status", "execution_result", "validation_result")
        self.assertEqual(
            tuple(executor_attempt[c] for c in columns),
            tuple(tool_attempt[c] for c in columns),
        )
        self.assertEqual(self.fx.status(self.key), self.fx.status(other))
        # ... but the persisted class and the ledger now tell them apart.
        executor_class = read_attempt_failure_class(
            self.fx.db_path, executor_attempt["attempt_id"]
        )
        tool_class = read_attempt_failure_class(self.fx.db_path, tool_attempt["attempt_id"])
        self.assertEqual(executor_class["failure_class"], FAILURE_CLASS_EXECUTION)
        self.assertEqual(tool_class["failure_class"], FAILURE_CLASS_TOOL_ERROR)
        self.assertEqual(
            self.ledger(self.key, executor_attempt["attempt_id"])["fields"][
                "failure_class"
            ]["value"],
            FAILURE_CLASS_EXECUTION,
        )
        self.assertEqual(
            self.ledger(other, tool_attempt["attempt_id"])["fields"]["failure_class"][
                "value"
            ],
            FAILURE_CLASS_TOOL_ERROR,
        )


class ExistingDatabaseTests(DispatcherFailureClassTestCase):
    """No migration: an existing DB copy runs unchanged, and old rows stay unknown."""

    @staticmethod
    def schema(db_path: Path) -> tuple[list, list]:
        with closing(sqlite3.connect(db_path)) as conn:
            objects = conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            migrations = conn.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            ).fetchall()
        return objects, migrations

    def test_fixture_db_copy_needs_no_schema_change_and_old_attempts_stay_unknown(
        self,
    ) -> None:
        # A fixture DB with an Attempt terminalized the way it was before this
        # change: a failed release whose metadata carries no failure class.
        self.fx.dispatch(self.key, RecordingExecutor())  # applies runtime schema
        legacy_key = self.fx.create_ticket("Pre-existing failure").task_key
        admission = RuntimeAdmissionStore(self.fx.db_path)
        claim = admission.claim(legacy_key, owner_id="old-runner")
        admission.release(
            claim.attempt_id,
            owner_id=claim.owner_id,
            lease_token=claim.lease_token,
            attempt_status="failed",
            task_status="failed",
            reason_code="runtime_failed",
            execution_result="failed",
        )

        # A consistent copy (the fixture DB runs in WAL mode).
        copy_path = self.fx.root / "fixture-copy.db"
        with closing(sqlite3.connect(self.fx.db_path)) as source, closing(
            sqlite3.connect(copy_path)
        ) as target:
            source.backup(target)
        before = self.schema(copy_path)
        # Run the new code on the copy twice: the schema never changes.
        self.fx.db_path = copy_path
        for label in ("first", "second"):
            key = self.fx.create_ticket(f"Copy run {label}").task_key
            self.fx.dispatch(key, RecordingExecutor("failed"))
            self.assert_class(FAILURE_CLASS_EXECUTION, FAILURE_EXECUTOR, task_key=key)
            self.assertEqual(self.schema(copy_path), before)

        old = self.last_attempt(legacy_key)
        self.assertIsNone(read_attempt_failure_class(copy_path, old["attempt_id"]))
        snapshot = outcome_ledger._read_snapshot(copy_path, old["attempt_id"])
        field = outcome_ledger._build_fields(snapshot, None)["failure_class"]
        self.assertIsNone(field["value"])
        self.assertEqual(field["provenance"], "unknown")
        self.assertEqual(field["reason"], "failure_class_not_recorded_on_terminal_event")


if __name__ == "__main__":
    unittest.main()
