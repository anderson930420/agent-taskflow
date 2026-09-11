"""FOLLOWUPS F1 acceptance gate: runtime progress wired into the execution loop.

Step 3 shipped the progress write surface (``RuntimeProgressStore``) and the
live board, but nothing in the runtime produced writes. F1 wires the producer:

* the Attempt reserved by the runtime-admission claim reaches
  ``ExecutorContext.attempt_id`` and ``ValidatorContext.attempt_id``;
* the dispatcher and the Level 2 approved-task runner record Prepare,
  Implementer and Validator transitions against that Attempt;
* a persisted ``created`` Ticket (display ``ready``) is runnable and claimable;
* ``blocked`` and ``paused`` never acquire work (SPEC §44).

Progress is observation, not lifecycle: a failing progress store must never
change a run's outcome.

Every fixture installs the lifecycle schema and Step 3's progress tables
explicitly, the way an operator must (Step 3 HANDOFF §2). Nothing here relies on
a migration running at dispatch time.
"""

from __future__ import annotations

import functools
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

import agent_taskflow.canonical_runtime_path as canonical_path
import agent_taskflow.runtime_progress_recorder as recorder_module
from agent_taskflow.approved_task_runner import run_approved_task
from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.realtime_projection import (
    BOARD_SECTION_RUNNING,
    build_board_projection,
    build_ticket_projection,
)
from agent_taskflow.runtime_admission import RuntimeAdmissionError, RuntimeAdmissionStore
from agent_taskflow.runtime_progress import find_progress_estimates
from agent_taskflow.runtime_progress_schema import migrate_runtime_progress
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.scheduler_execution_engine_request_builder import (
    SchedulerExecutionEngineRequestBuildInput,
    build_scheduler_execution_engine_request,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult


WIRED_STEPS = ("Prepare", "Implementer", "Validator")
UNWIRED_STEPS = ("Scout", "Planner", "Reviewer", "Integration")


# -- helpers -----------------------------------------------------------------


def _count(db_path: Path, sql: str, params: tuple[Any, ...]) -> int:
    """Run a COUNT(*) query, treating a missing table as zero rows."""

    with closing(sqlite3.connect(db_path)) as conn:
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def attempt_count(db_path: Path, task_key: str) -> int:
    return _count(
        db_path,
        "SELECT COUNT(*) FROM attempts JOIN tasks ON tasks.task_id = attempts.task_id "
        "WHERE tasks.task_key = ?",
        (task_key,),
    )


def lease_count(db_path: Path, task_key: str) -> int:
    return _count(
        db_path,
        "SELECT COUNT(*) FROM runtime_leases "
        "JOIN tasks ON tasks.task_id = runtime_leases.task_id "
        "WHERE tasks.task_key = ?",
        (task_key,),
    )


def claim_count(db_path: Path, task_key: str) -> int:
    # Lease heartbeats are also lifecycle events, recorded as preparing ->
    # preparing; only a transition *into* preparing is a claim.
    return _count(
        db_path,
        "SELECT COUNT(*) FROM lifecycle_events "
        "JOIN tasks ON tasks.task_id = lifecycle_events.task_id "
        "WHERE tasks.task_key = ? AND lifecycle_events.to_status = 'preparing' "
        "AND lifecycle_events.from_status IS NOT 'preparing'",
        (task_key,),
    )


def table_exists(db_path: Path, name: str) -> bool:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    return row is not None


def status_sequence(store: TaskMirrorStore, task_key: str) -> list[str]:
    import json

    statuses: list[str] = []
    for event in store.list_task_events(task_key):
        if event.event_type != "status_changed" or not event.payload_json:
            continue
        statuses.append(json.loads(event.payload_json).get("status"))
    return statuses


def step_statuses(snapshot: Any) -> dict[str, str]:
    return {step.name: step.status for step in snapshot.ordered_steps()}


def board_ticket(db_path: Path, task_key: str) -> Any:
    board = build_board_projection(db_path)
    for section in board.sections:
        for ticket in section.tickets:
            if ticket.task_key == task_key:
                return ticket
    for ticket in board.unsectioned:
        if ticket.task_key == task_key:
            return ticket
    raise AssertionError(f"{task_key} is not on the board")


class RecordingProgressStore(RuntimeProgressStore):
    """Real progress store that also records every write, in call order.

    ``record_step`` notes the step status the store held *before* the write, so
    a test can read the full ``pending -> running -> passed`` history even
    though the table keeps only the latest status per step.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.transitions: list[tuple[str, str, str, str]] = []
        self.activities: list[tuple[str, str | None, str | None]] = []
        self.payloads: list[dict[str, Any]] = []

    def record_step(self, *, attempt_id, step, status, summary=None, metadata=None):
        snapshot = self.get_attempt_progress(attempt_id)
        before = snapshot.step_status(step) if snapshot is not None else "pending"
        self.transitions.append((attempt_id, step, before, status))
        self.payloads.append({"summary": summary, "metadata": dict(metadata or {})})
        return super().record_step(
            attempt_id=attempt_id,
            step=step,
            status=status,
            summary=summary,
            metadata=metadata,
        )

    def set_current_activity(self, *, attempt_id, phase=None, activity=None):
        self.activities.append((attempt_id, phase, activity))
        self.payloads.append({"current_phase": phase, "current_activity": activity})
        return super().set_current_activity(
            attempt_id=attempt_id, phase=phase, activity=activity
        )

    def history(self, step: str) -> list[tuple[str, str]]:
        return [(before, after) for _, name, before, after in self.transitions if name == step]


class RaisingProgressStore:
    """A progress store whose every write fails."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls = 0

    def record_step(self, **_kwargs: Any) -> None:
        self.calls += 1
        raise sqlite3.OperationalError("progress store is unavailable")

    def set_current_activity(self, **_kwargs: Any) -> None:
        self.calls += 1
        raise RuntimeError("progress store is unavailable")


class SnapshotExecutor(Executor):
    """Fake executor that captures its context and the live progress state."""

    def __init__(
        self,
        db_path: Path,
        *,
        name: str = "fake",
        status: str = "completed",
        summary: str | None = None,
        raises: Exception | None = None,
        write_codex_evidence: bool = False,
    ) -> None:
        self.db_path = db_path
        self.name = name
        self.status = status
        self.summary = summary
        self.raises = raises
        self.write_codex_evidence = write_codex_evidence
        self.contexts: list[ExecutorContext] = []
        self.progress_during_run: Any = None
        self.board_ticket_during_run: Any = None

    def run(self, context: ExecutorContext) -> ExecutorResult:
        self.contexts.append(context)
        if context.attempt_id is not None and table_exists(self.db_path, "attempt_progress"):
            self.progress_during_run = RuntimeProgressStore(
                self.db_path
            ).get_attempt_progress(context.attempt_id)
            self.board_ticket_during_run = board_ticket(self.db_path, context.task_key)
        if self.write_codex_evidence:
            from agent_taskflow.codex_advisory_review import (
                CodexAdvisoryReviewRequest,
                generate_codex_advisory_review,
            )

            generate_codex_advisory_review(
                CodexAdvisoryReviewRequest(
                    task_key=context.task_key,
                    artifact_dir=context.artifact_dir,
                    dry_run=True,
                )
            )
        if self.raises is not None:
            raise self.raises
        return ExecutorResult(
            executor=self.name,
            status=self.status,
            exit_code=0 if self.status == "completed" else 1,
            summary=self.summary or f"executor {self.status}",
        )


class SnapshotValidator(Validator):
    def __init__(self, db_path: Path, name: str, status: str = "passed") -> None:
        self.db_path = db_path
        self.name = name
        self.status = status
        self.contexts: list[ValidatorContext] = []
        self.progress_during_run: Any = None

    def run(self, context: ValidatorContext) -> ValidatorResult:
        self.contexts.append(context)
        if context.attempt_id is not None and table_exists(self.db_path, "attempt_progress"):
            self.progress_during_run = RuntimeProgressStore(
                self.db_path
            ).get_attempt_progress(context.attempt_id)
        return ValidatorResult(
            validator=self.name,
            status=self.status,
            exit_code=0 if self.status in {"passed", "skipped"} else 1,
            summary=f"validator {self.status}",
        )


# -- dispatcher path -----------------------------------------------------------


class DispatcherWiringTestCase(unittest.TestCase):
    """Plain dispatcher path on a non-Git repo: canonical token claim."""

    TASK_KEY = "AT-0101"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / self.TASK_KEY
        self.artifact_dir = self.root / "artifacts" / self.TASK_KEY
        self.worktree_path.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def install_progress_schema(self) -> None:
        # Operator-run migrations, never implied by dispatch.
        migrate_task_attempt_lifecycle(self.db_path)
        migrate_runtime_progress(self.db_path)

    def add_task(self, status: str, *, blocked_reason: str | None = None) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=self.TASK_KEY,
                project="forms",
                board="forms",
                title="Separate ending-page image",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
                blocked_reason=blocked_reason,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self.TASK_KEY,
                repo_path=self.repo_path,
                worktree_path=self.worktree_path,
                branch=f"task/{self.TASK_KEY}",
                base_branch="main",
                status="active",
            )
        )

    def make_dispatcher(
        self,
        *,
        executor: SnapshotExecutor | None = None,
        validators: dict[str, SnapshotValidator] | None = None,
        progress_store: Any = None,
        default_executor: str = "fake",
    ) -> Dispatcher:
        self.executor = executor or SnapshotExecutor(self.db_path)
        self.validators = validators or {
            "pytest": SnapshotValidator(self.db_path, "pytest"),
            "openspec": SnapshotValidator(self.db_path, "openspec", "skipped"),
        }
        return Dispatcher(
            self.store,
            executor_registry={"fake": self.executor},
            validator_registry=self.validators,
            validators=tuple(self.validators),
            default_executor=default_executor,
            default_model="fake-model",
            progress_store=progress_store,
        )

    def only_attempt_id(self) -> str:
        attempts = AttemptStore(self.db_path).list_attempts(self.TASK_KEY)
        self.assertEqual(len(attempts), 1)
        return attempts[0].attempt_id


class CreatedTicketStartsTests(DispatcherWiringTestCase):
    """Acceptance 1: a Step 1 Ticket (persisted ``created``) can start."""

    def test_created_ticket_is_claimed_once_and_attempt_reaches_both_contexts(self) -> None:
        self.install_progress_schema()
        self.add_task("created")
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "waiting_approval", result.summary)
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(claim_count(self.db_path, self.TASK_KEY), 1)
        self.assertEqual(lease_count(self.db_path, self.TASK_KEY), 1)
        attempt_id = self.only_attempt_id()

        self.assertEqual(len(self.executor.contexts), 1)
        self.assertEqual(self.executor.contexts[0].attempt_id, attempt_id)
        for name, validator in self.validators.items():
            with self.subTest(validator=name):
                self.assertEqual(len(validator.contexts), 1)
                self.assertEqual(validator.contexts[0].attempt_id, attempt_id)

    def test_created_ticket_is_claimable_by_runtime_admission(self) -> None:
        self.add_task("created")

        claim = RuntimeAdmissionStore(self.db_path).claim(
            self.TASK_KEY, owner_id="f1-test"
        )

        self.assertEqual(claim.task_key, self.TASK_KEY)
        self.assertEqual(attempt_count(self.db_path, self.TASK_KEY), 1)
        self.assertEqual(lease_count(self.db_path, self.TASK_KEY), 1)
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "preparing")


class ProgressIsRecordedTests(DispatcherWiringTestCase):
    """Acceptance 2: Prepare, Implementer, Validator go pending -> running -> passed."""

    def run_created_ticket(self) -> tuple[RecordingProgressStore, str]:
        self.install_progress_schema()
        self.add_task("created")
        progress = RecordingProgressStore(self.db_path)
        dispatcher = self.make_dispatcher(progress_store=progress)
        result = dispatcher.dispatch_task(self.TASK_KEY)
        self.assertEqual(result.status, "waiting_approval", result.summary)
        return progress, self.only_attempt_id()

    def test_each_wired_step_goes_pending_running_passed_in_order(self) -> None:
        progress, attempt_id = self.run_created_ticket()

        for step in WIRED_STEPS:
            with self.subTest(step=step):
                self.assertEqual(
                    progress.history(step),
                    [("pending", "running"), ("running", "passed")],
                )
        self.assertEqual(
            {entry[0] for entry in progress.transitions}, {attempt_id}
        )
        order = [(step, after) for _, step, _, after in progress.transitions]
        self.assertEqual(
            order,
            [
                ("Prepare", "running"),
                ("Prepare", "passed"),
                ("Implementer", "running"),
                ("Implementer", "passed"),
                ("Validator", "running"),
                ("Validator", "passed"),
            ],
        )

        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(attempt_id)
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        for step in WIRED_STEPS:
            self.assertEqual(statuses[step], "passed")
        for step in UNWIRED_STEPS:
            self.assertEqual(statuses[step], "pending")

    def test_live_state_during_executor_and_validator_runs(self) -> None:
        self.run_created_ticket()

        during_executor = self.executor.progress_during_run
        self.assertIsNotNone(during_executor)
        self.assertEqual(
            {k: v for k, v in step_statuses(during_executor).items() if k in WIRED_STEPS},
            {"Prepare": "passed", "Implementer": "running", "Validator": "pending"},
        )
        self.assertEqual(during_executor.current_phase, "Implementer")
        self.assertTrue(during_executor.current_activity)

        during_validator = self.validators["pytest"].progress_during_run
        self.assertIsNotNone(during_validator)
        self.assertEqual(
            {k: v for k, v in step_statuses(during_validator).items() if k in WIRED_STEPS},
            {"Prepare": "passed", "Implementer": "passed", "Validator": "running"},
        )
        self.assertEqual(during_validator.current_phase, "Validator")

    def test_current_phase_and_activity_change_at_each_transition(self) -> None:
        progress, attempt_id = self.run_created_ticket()

        phases = [phase for _, phase, _ in progress.activities]
        self.assertEqual(phases[0], "Prepare")
        self.assertIn("Implementer", phases)
        self.assertEqual(phases[-1], "Validator")
        activities = [activity for _, _, activity in progress.activities]
        self.assertTrue(all(activities))
        for earlier, later in zip(activities, activities[1:]):
            self.assertNotEqual(earlier, later)

        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(attempt_id)
        assert snapshot is not None
        self.assertEqual(snapshot.current_phase, "Validator")
        self.assertEqual(snapshot.current_activity, activities[-1])

    def test_step3_board_and_ticket_projection_show_the_progress(self) -> None:
        _progress, attempt_id = self.run_created_ticket()

        during = self.executor.board_ticket_during_run
        self.assertIsNotNone(during)
        self.assertEqual(during.section, BOARD_SECTION_RUNNING)
        self.assertEqual(during.attempt_id, attempt_id)
        self.assertEqual(
            {step.name: step.status for step in during.steps}["Implementer"],
            "running",
        )

        after = board_ticket(self.db_path, self.TASK_KEY)
        self.assertEqual(after.attempt_id, attempt_id)
        board_steps = {step.name: step.status for step in after.steps}
        for step in WIRED_STEPS:
            self.assertEqual(board_steps[step], "passed")
        self.assertEqual(after.current_phase, "Validator")

        projection = build_ticket_projection(self.db_path, self.TASK_KEY)
        assert projection is not None
        self.assertEqual(projection.selected_attempt_id, attempt_id)
        ticket_steps = {step.name: step.status for step in projection.ticket.steps}
        for step in WIRED_STEPS:
            self.assertEqual(ticket_steps[step], "passed")
        for step in UNWIRED_STEPS:
            self.assertEqual(ticket_steps[step], "pending")


class FailureIsVisibleTests(DispatcherWiringTestCase):
    """Acceptance 3: a failing executor leaves Implementer failed, Validator pending."""

    def test_failing_executor_leaves_implementer_failed_and_validator_pending(self) -> None:
        self.install_progress_schema()
        self.add_task("created")
        dispatcher = self.make_dispatcher(
            executor=SnapshotExecutor(self.db_path, status="failed", summary="tests failed")
        )

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(
            self.only_attempt_id()
        )
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        self.assertEqual(statuses["Prepare"], "passed")
        self.assertEqual(statuses["Implementer"], "failed")
        self.assertEqual(statuses["Validator"], "pending")
        self.assertEqual(snapshot.current_phase, "Implementer")
        for validator in self.validators.values():
            self.assertEqual(validator.contexts, [])

    def test_raising_executor_leaves_implementer_failed(self) -> None:
        self.install_progress_schema()
        self.add_task("queued")
        dispatcher = self.make_dispatcher(
            executor=SnapshotExecutor(self.db_path, raises=RuntimeError("boom"))
        )

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(
            self.only_attempt_id()
        )
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        self.assertEqual(statuses["Implementer"], "failed")
        self.assertEqual(statuses["Validator"], "pending")

    def test_failing_validator_leaves_validator_failed(self) -> None:
        self.install_progress_schema()
        self.add_task("queued")
        dispatcher = self.make_dispatcher(
            validators={"pytest": SnapshotValidator(self.db_path, "pytest", "failed")}
        )

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(
            self.only_attempt_id()
        )
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        self.assertEqual(statuses["Implementer"], "passed")
        self.assertEqual(statuses["Validator"], "failed")

    def test_unavailable_executor_leaves_prepare_failed(self) -> None:
        self.install_progress_schema()
        self.add_task("queued")
        dispatcher = self.make_dispatcher(default_executor="does-not-exist")

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        self.assertIn("is unavailable", result.blocked_reason or "")
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(
            self.only_attempt_id()
        )
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        self.assertEqual(statuses["Prepare"], "failed")
        self.assertEqual(statuses["Implementer"], "pending")
        self.assertEqual(statuses["Validator"], "pending")


class RefusedStatusAssertions:
    """Shared assertions for statuses that must never acquire work (§44)."""

    status: str

    def assert_no_runtime_ownership(self: Any) -> None:
        self.assertEqual(attempt_count(self.db_path, self.TASK_KEY), 0)
        self.assertEqual(lease_count(self.db_path, self.TASK_KEY), 0)

    def test_dispatch_refuses_without_attempt_or_lease(self: Any) -> None:
        self.install_progress_schema()
        self.add_task(self.status, blocked_reason="waiting on AT-0100")
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        self.assertIn(self.status, result.summary)
        self.assertEqual(self.executor.contexts, [])
        for validator in self.validators.values():
            self.assertEqual(validator.contexts, [])
        self.assert_no_runtime_ownership()

    def test_runtime_admission_claim_refuses(self: Any) -> None:
        self.add_task(self.status, blocked_reason="waiting on AT-0100")

        with self.assertRaisesRegex(RuntimeAdmissionError, "not claimable"):
            RuntimeAdmissionStore(self.db_path).claim(self.TASK_KEY, owner_id="f1-test")
        with self.assertRaisesRegex(RuntimeAdmissionError, "not claimable"):
            canonical_path.CanonicalRuntimeAdmissionStore(self.db_path).claim(
                self.TASK_KEY, owner_id="f1-test"
            )

        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, self.status)
        self.assert_no_runtime_ownership()

    def test_runtime_store_preparing_transition_refuses(self: Any) -> None:
        self.add_task(self.status, blocked_reason="waiting on AT-0100")
        runtime_store = canonical_path.canonical_runtime_task_store(self.db_path)
        self.addCleanup(runtime_store.shutdown_runtime_supervisors)

        with self.assertRaises(RuntimeAdmissionError):
            runtime_store.update_task_status(
                self.TASK_KEY, "preparing", source="f1-test"
            )

        self.assertIsNone(runtime_store.runtime_claim(self.TASK_KEY))
        self.assert_no_runtime_ownership()


class BlockedTicketCannotExecuteTests(RefusedStatusAssertions, DispatcherWiringTestCase):
    """Acceptance 4: §44 Blocked Ticket cannot execute."""

    status = "blocked"


class PausedTicketCannotAcquireWorkTests(RefusedStatusAssertions, DispatcherWiringTestCase):
    """Acceptance 5: §44 Paused Ticket cannot acquire work."""

    status = "paused"


def task_row(db_path: Path, task_key: str) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
    assert row is not None
    return dict(row)


class RefusalLeavesRowUntouchedTests(DispatcherWiringTestCase):
    """Ruling 10c: refusing ``blocked`` / ``paused`` writes nothing at all."""

    def dispatch_and_compare(self, status: str, blocked_reason: str | None) -> Any:
        self.install_progress_schema()
        self.add_task(status, blocked_reason=blocked_reason)
        # The dispatch entry's runtime-control check runs the lifecycle
        # migration, which backfills a missing `task_id` on every row whatever
        # its status. Apply that one-time backfill first so the comparison
        # below covers the whole row.
        migrate_task_attempt_lifecycle(self.db_path)
        before_row = task_row(self.db_path, self.TASK_KEY)
        before_events = len(self.store.list_task_events(self.TASK_KEY))
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.summary, f"Task status is not runnable: {status}")
        self.assertEqual(result.blocked_reason, f"Task status is not runnable: {status}")
        self.assertEqual(task_row(self.db_path, self.TASK_KEY), before_row)
        self.assertEqual(len(self.store.list_task_events(self.TASK_KEY)), before_events)
        self.assertEqual(attempt_count(self.db_path, self.TASK_KEY), 0)
        self.assertEqual(lease_count(self.db_path, self.TASK_KEY), 0)
        return self.store.get_task(self.TASK_KEY)

    def test_dispatching_blocked_keeps_the_original_blocked_reason(self) -> None:
        task = self.dispatch_and_compare("blocked", "tests failed on AT-0101")

        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "tests failed on AT-0101")

    def test_dispatching_paused_leaves_it_paused(self) -> None:
        task = self.dispatch_and_compare("paused", None)

        self.assertEqual(task.status, "paused")
        self.assertIsNone(task.blocked_reason)

    def test_other_non_runnable_statuses_keep_the_blocking_refusal(self) -> None:
        # Only blocked and paused are refused without a write; every other
        # non-runnable status keeps today's branch.
        self.install_progress_schema()
        self.add_task("unknown")

        result = self.make_dispatcher().dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "Task status is not runnable: unknown")
        self.assertEqual(attempt_count(self.db_path, self.TASK_KEY), 0)


class LegacyQueuedRegressionTests(DispatcherWiringTestCase):
    """Acceptance 6: a ``queued`` legacy task runs exactly as before."""

    def assert_legacy_outcome(self, result: Any) -> None:
        self.assertEqual(result.task_key, self.TASK_KEY)
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(
            result.summary,
            "Task dispatched successfully and is waiting for human approval.",
        )
        self.assertEqual(result.executor_status, "completed")
        self.assertEqual(result.validator_statuses, {"pytest": "passed", "openspec": "skipped"})
        self.assertIsNone(result.blocked_reason)
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertIsNone(task.blocked_reason)
        self.assertEqual(
            status_sequence(self.store, self.TASK_KEY),
            ["preparing", "implementing", "validating", "waiting_approval"],
        )
        self.assertEqual(len(self.store.list_validation_results(self.TASK_KEY)), 2)
        self.assertEqual(claim_count(self.db_path, self.TASK_KEY), 1)

    def test_queued_task_runs_to_waiting_approval_with_progress(self) -> None:
        self.install_progress_schema()
        self.add_task("queued")

        result = self.make_dispatcher().dispatch_task(self.TASK_KEY)

        self.assert_legacy_outcome(result)
        attempt_id = self.only_attempt_id()
        self.assertEqual(self.executor.contexts[0].attempt_id, attempt_id)
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(attempt_id)
        assert snapshot is not None
        self.assertEqual(step_statuses(snapshot)["Validator"], "passed")

    def test_queued_task_runs_unchanged_when_progress_tables_are_absent(self) -> None:
        # An operator DB that never ran Step 3's migration. Dispatch must not
        # migrate it, and the missing tables must not change the outcome.
        self.add_task("queued")

        with self.assertLogs(recorder_module.__name__, level="WARNING") as logs:
            result = self.make_dispatcher().dispatch_task(self.TASK_KEY)

        self.assert_legacy_outcome(result)
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1, "one warning per run, not per write")
        self.assertIn("no such table", warnings[0].getMessage())
        self.assertFalse(table_exists(self.db_path, "attempt_progress"))
        self.assertFalse(table_exists(self.db_path, "attempt_observed_steps"))


class ProgressIsNotLifecycleTests(DispatcherWiringTestCase):
    """Acceptance 7: a raising progress store does not change the outcome."""

    def test_success_outcome_is_unchanged_by_a_raising_progress_store(self) -> None:
        self.install_progress_schema()
        self.add_task("created")
        progress = RaisingProgressStore()
        dispatcher = self.make_dispatcher(progress_store=progress)

        with self.assertLogs(recorder_module.__name__, level="WARNING"):
            result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertGreater(progress.calls, 0)
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.executor_status, "completed")
        self.assertIsNone(result.blocked_reason)
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(
            status_sequence(self.store, self.TASK_KEY),
            ["preparing", "implementing", "validating", "waiting_approval"],
        )
        attempt = AttemptStore(self.db_path).get_attempt(self.only_attempt_id())
        assert attempt is not None
        self.assertFalse(attempt.is_active)
        self.assertEqual(attempt.validation_result, "passed")

    def test_failure_outcome_is_unchanged_by_a_raising_progress_store(self) -> None:
        self.install_progress_schema()
        self.add_task("queued")
        progress = RaisingProgressStore()
        dispatcher = self.make_dispatcher(
            executor=SnapshotExecutor(self.db_path, status="failed", summary="tests failed"),
            progress_store=progress,
        )

        with self.assertLogs(recorder_module.__name__, level="WARNING"):
            result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertGreater(progress.calls, 0)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_reason, "tests failed")
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "tests failed")


class NoProgressEstimateTests(DispatcherWiringTestCase):
    """Acceptance 9: §14.2 — no percentage, ETA or completion estimate."""

    def test_progress_text_and_payload_carry_no_estimate(self) -> None:
        self.install_progress_schema()
        self.add_task("queued")
        progress = RecordingProgressStore(self.db_path)
        # The executor's own summary is full of estimates. None of it may leak
        # into progress, and its presence must not stop progress being written.
        dispatcher = self.make_dispatcher(
            executor=SnapshotExecutor(
                self.db_path,
                status="failed",
                summary="85% done, ETA 2 minutes remaining",
            ),
            progress_store=progress,
        )

        result = dispatcher.dispatch_task(self.TASK_KEY)

        self.assertEqual(result.status, "blocked")
        self.assertTrue(progress.payloads)
        self.assertEqual(find_progress_estimates(progress.payloads), ())
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(
            self.only_attempt_id()
        )
        assert snapshot is not None
        self.assertEqual(step_statuses(snapshot)["Implementer"], "failed")
        self.assertEqual(
            find_progress_estimates(
                {
                    "current_phase": snapshot.current_phase,
                    "current_activity": snapshot.current_activity,
                    "steps": [
                        {"summary": step.summary, "metadata": dict(step.metadata)}
                        for step in snapshot.steps
                    ],
                }
            ),
            (),
        )

    def test_successful_run_progress_carries_no_estimate(self) -> None:
        self.install_progress_schema()
        self.add_task("created")
        progress = RecordingProgressStore(self.db_path)

        self.make_dispatcher(progress_store=progress).dispatch_task(self.TASK_KEY)

        self.assertTrue(progress.payloads)
        self.assertEqual(find_progress_estimates(progress.payloads), ())


# -- Level 2 ExecutionEngine path ---------------------------------------------


class Level2ExecutionEnginePathTests(unittest.TestCase):
    """Acceptance 8: the reserved Attempt reaches the executor and gets progress."""

    TASK_KEY = "AT-F1-LEVEL2"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.artifacts = self.root / "artifacts"
        self.repo.mkdir()
        self.artifacts.mkdir()
        self._init_repo()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key=self.TASK_KEY,
                project="agent-taskflow",
                board="agent-taskflow",
                title="F1 Level 2 wiring",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifacts,
            )
        )
        migrate_task_attempt_lifecycle(self.db_path)
        migrate_runtime_progress(self.db_path)

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _init_repo(self) -> None:
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("f1\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial commit")

    def execute(self, executor: SnapshotExecutor, validator: SnapshotValidator) -> Any:
        request = build_scheduler_execution_engine_request(
            SchedulerExecutionEngineRequestBuildInput(
                task_key=self.TASK_KEY,
                repo="anderson930420/agent-taskflow",
                local_repo_path=self.repo,
                artifact_dir=self.artifacts,
                executor="noop",
                validators=("policy",),
                lifecycle_db_path=self.db_path,
                dry_run=False,
                confirmed=True,
                preflight=False,
                runtime_handoff_path=self.root / "handoff.json",
            )
        )
        runner = functools.partial(
            run_approved_task,
            executor_registry={"noop": executor},
            validator_registry={"policy": validator},
        )
        return ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=runner
        ).execute(request)

    def test_reserved_attempt_reaches_contexts_and_gets_progress(self) -> None:
        executor = SnapshotExecutor(self.db_path, name="noop", write_codex_evidence=True)
        validator = SnapshotValidator(self.db_path, "policy")

        result = self.execute(executor, validator)

        self.assertTrue(result.ok, result.summary)
        self.assertTrue(result.metadata["canonical_attempt_bound"])
        attempt_id = result.metadata["canonical_attempt_id"]
        self.assertTrue(attempt_id)
        self.assertEqual(len(executor.contexts), 1)
        self.assertEqual(executor.contexts[0].attempt_id, attempt_id)
        self.assertEqual(len(validator.contexts), 1)
        self.assertEqual(validator.contexts[0].attempt_id, attempt_id)

        during = executor.progress_during_run
        self.assertIsNotNone(during)
        self.assertEqual(during.attempt_id, attempt_id)
        self.assertEqual(during.step_status("Implementer"), "running")

        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(attempt_id)
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        for step in WIRED_STEPS:
            self.assertEqual(statuses[step], "passed")
        for step in UNWIRED_STEPS:
            self.assertEqual(statuses[step], "pending")
        self.assertEqual(snapshot.current_phase, "Validator")
        latest = RuntimeProgressStore(self.db_path).get_latest_attempt_progress(self.TASK_KEY)
        assert latest is not None
        self.assertEqual(latest.attempt_id, attempt_id)

    def test_failing_executor_is_visible_on_the_reserved_attempt(self) -> None:
        executor = SnapshotExecutor(self.db_path, name="noop", status="failed")
        validator = SnapshotValidator(self.db_path, "policy")

        result = self.execute(executor, validator)

        self.assertFalse(result.ok)
        attempt_id = result.metadata["canonical_attempt_id"]
        self.assertEqual(executor.contexts[0].attempt_id, attempt_id)
        snapshot = RuntimeProgressStore(self.db_path).get_attempt_progress(attempt_id)
        assert snapshot is not None
        statuses = step_statuses(snapshot)
        self.assertEqual(statuses["Prepare"], "passed")
        self.assertEqual(statuses["Implementer"], "failed")
        self.assertEqual(statuses["Validator"], "pending")

    def test_raising_progress_store_does_not_change_level2_outcome(self) -> None:
        executor = SnapshotExecutor(self.db_path, name="noop", write_codex_evidence=True)
        validator = SnapshotValidator(self.db_path, "policy")

        with mock.patch.object(
            recorder_module, "RuntimeProgressStore", RaisingProgressStore
        ), self.assertLogs(recorder_module.__name__, level="WARNING"):
            result = self.execute(executor, validator)

        self.assertTrue(result.ok, result.summary)
        self.assertTrue(result.metadata["canonical_attempt_bound"])
        self.assertEqual(
            executor.contexts[0].attempt_id, result.metadata["canonical_attempt_id"]
        )
        task = self.store.get_task(self.TASK_KEY)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")


if __name__ == "__main__":
    unittest.main()
