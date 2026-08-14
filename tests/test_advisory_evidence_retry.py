"""Tests for the audited operator advisory-evidence retry recovery path.

The v0.2.5 required Codex advisory evidence gate blocks ``run_approved_task``
at the ``codex_advisory_evidence`` phase when the advisory artifact is missing.
Because the advisory artifact can only be produced *after* attempt evidence
exists, and ``reset_task_status.py`` reserves a *new* Attempt with a *new*
artifact dir, an otherwise complete task can be stuck in ``blocked`` forever.

These tests prove the recovery entry point:

- every precondition failure is reported and never mutates state;
- the happy path performs the same ``blocked -> waiting_approval`` transition
  the runner would have performed and records an explicit audit event;
- the recovery path never weakens the gate, approves, merges, cleans up, or
  invokes a subprocess.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.advisory_evidence_retry import (
    CHECK_ADVISORY_EVIDENCE,
    CHECK_EXECUTOR_EVIDENCE,
    CHECK_PYTEST_EVIDENCE,
    CHECK_TASK_BLOCKED,
    PYTEST_LAUNCH_SPEC_FILENAME,
    PYTEST_LOG_FILENAME,
    RETRY_AUDIT_KIND,
    RETRY_FROM_STATUS,
    RETRY_REASON,
    RETRY_SOURCE,
    RETRY_TO_STATUS,
    AdvisoryEvidenceRetryError,
    AdvisoryEvidenceRetryRequest,
    run_advisory_evidence_retry,
    summarize_pytest_log,
)
from agent_taskflow.codex_advisory_review import (
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    REVIEWER,
    SCHEMA_VERSION,
    STDERR_FILENAME,
    STDOUT_FILENAME,
    build_default_checklist,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import retry_advisory_evidence_transition as script


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_SOURCE = (
    REPO_ROOT / "agent_taskflow" / "advisory_evidence_retry.py"
).read_text(encoding="utf-8")
SCRIPT_SOURCE = (
    REPO_ROOT / "scripts" / "retry_advisory_evidence_transition.py"
).read_text(encoding="utf-8")

PASSING_PYTEST_LOG = """Validator: pytest
Command: ['python3', '-m', 'pytest']
Worktree: /tmp/worktree
Environment: not logged

============================= test session starts ==============================
collected 4307 items

tests/test_store.py ....................                                 [ 50%]
tests/test_tasks.py ....................                                 [100%]

================= 4299 passed, 8 skipped in 373.48s (0:06:13) ==================
"""

FAILING_PYTEST_LOG = """============================= test session starts ==============================
collected 4307 items

tests/test_store.py ....F...............                                 [100%]

=========================== short test summary info ============================
FAILED tests/test_store.py::test_thing - AssertionError
============ 1 failed, 4298 passed, 8 skipped in 371.02s (0:06:11) =============
"""


def _advisory_payload(task_key: str, artifact_dir: Path, **overrides) -> dict:
    """Build a contract-valid confirm-run advisory payload for ``task_key``."""

    base = {
        "schema_version": SCHEMA_VERSION,
        "reviewer": REVIEWER,
        "task_key": task_key,
        "review_status": "looks_good",
        "risk_level": "low",
        "validation_authority": False,
        "human_review_required": True,
        "summary": "",
        "dry_run": False,
        "confirm_run": True,
        "codex_cli_invoked": True,
        "tool_error": None,
        "generated_at": "2026-08-14T00:00:00Z",
        "artifacts": {
            "codex_outputs": {
                STDOUT_FILENAME: str(artifact_dir / STDOUT_FILENAME),
                STDERR_FILENAME: str(artifact_dir / STDERR_FILENAME),
            }
        },
        "review_checklist": build_default_checklist(status="pass", summary="ok"),
        "human_review_priorities": [
            {
                "priority": 1,
                "area": "design_risk",
                "reason": "confirm the design risk is acceptable",
                "suggested_checks": ["review design findings"],
            }
        ],
    }
    base.update(overrides)
    return base


class AdvisoryEvidenceRetryTestCase(unittest.TestCase):
    """Shared fixture: a blocked task with a complete Attempt artifact dir."""

    task_key = "AT-GH-159"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.db"
        self.artifact_dir = (
            self.root / "artifacts" / self.task_key / "attempt-3e1b6593416b"
        )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- fixture builders -------------------------------------------------

    def _seed_task(self, *, status: str = RETRY_FROM_STATUS) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="github-issue-scheduler",
                board="agent-taskflow",
                title="Advisory evidence retry candidate",
                status=status,
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )

    def _write_executor_evidence(self) -> None:
        (self.artifact_dir / "executor-launch-spec-pi.json").write_text(
            json.dumps({"executor_name": "pi"}), encoding="utf-8"
        )
        (self.artifact_dir / "pi-executor.log").write_text(
            "executor ran\n", encoding="utf-8"
        )

    def _write_pytest_evidence(self, log_text: str = PASSING_PYTEST_LOG) -> None:
        (self.artifact_dir / PYTEST_LOG_FILENAME).write_text(
            log_text, encoding="utf-8"
        )
        (self.artifact_dir / PYTEST_LAUNCH_SPEC_FILENAME).write_text(
            json.dumps({"validator_name": "pytest"}), encoding="utf-8"
        )

    def _write_advisory_evidence(
        self, *, artifact_task_key: str | None = None, **overrides
    ) -> None:
        payload = _advisory_payload(
            artifact_task_key or self.task_key, self.artifact_dir, **overrides
        )
        (self.artifact_dir / JSON_FILENAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )
        (self.artifact_dir / MARKDOWN_FILENAME).write_text(
            "# Codex Advisory Review\n", encoding="utf-8"
        )
        (self.artifact_dir / STDOUT_FILENAME).write_text("out\n", encoding="utf-8")
        (self.artifact_dir / STDERR_FILENAME).write_text("err\n", encoding="utf-8")

    def _seed_complete_attempt(self, *, status: str = RETRY_FROM_STATUS) -> None:
        self._seed_task(status=status)
        self._write_executor_evidence()
        self._write_pytest_evidence()
        self._write_advisory_evidence()

    # -- helpers ----------------------------------------------------------

    def _run(self, *, confirm_transition: bool = False, task_key: str | None = None):
        return run_advisory_evidence_retry(
            AdvisoryEvidenceRetryRequest(
                task_key=task_key or self.task_key,
                db_path=self.db_path,
                artifact_dir=self.artifact_dir,
                operator="operator@example.com",
                confirm_transition=confirm_transition,
            ),
            store=self.store,
        )

    def _check(self, result, name):
        for check in result.checks:
            if check.name == name:
                return check
        self.fail(f"missing precondition check {name!r}")

    def _current_status(self) -> str:
        task = self.store.get_task(self.task_key)
        assert task is not None
        return task.status

    def _audit_events(self) -> list[dict]:
        payloads = []
        for event in self.store.list_task_events(self.task_key):
            if event.payload_json is None:
                continue
            payload = json.loads(event.payload_json)
            if payload.get("kind") == RETRY_AUDIT_KIND:
                payloads.append(payload)
        return payloads


class PreconditionFailureTests(AdvisoryEvidenceRetryTestCase):
    def test_task_not_found_blocks_and_does_not_mutate(self) -> None:
        self._write_executor_evidence()
        self._write_pytest_evidence()
        self._write_advisory_evidence(artifact_task_key="AT-GH-404")

        result = self._run(confirm_transition=True, task_key="AT-GH-404")

        self.assertFalse(result.preconditions_satisfied)
        self.assertFalse(result.mutated)
        self.assertFalse(result.audit_event_recorded)
        self.assertIsNone(result.observed_status)
        status_check = self._check(result, CHECK_TASK_BLOCKED)
        self.assertFalse(status_check.satisfied)
        self.assertFalse(status_check.details["task_found"])
        self.assertIn("was not found", status_check.summary)

    def test_wrong_status_blocks_and_does_not_mutate(self) -> None:
        self._seed_complete_attempt(status="waiting_approval")

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        self.assertFalse(result.mutated)
        self.assertEqual(result.observed_status, "waiting_approval")
        status_check = self._check(result, CHECK_TASK_BLOCKED)
        self.assertFalse(status_check.satisfied)
        self.assertIn("expected 'blocked'", status_check.summary)
        self.assertEqual(self._current_status(), "waiting_approval")
        self.assertEqual(self._audit_events(), [])

    def test_missing_executor_evidence_blocks(self) -> None:
        self._seed_task()
        self._write_pytest_evidence()
        self._write_advisory_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        self.assertFalse(result.mutated)
        executor_check = self._check(result, CHECK_EXECUTOR_EVIDENCE)
        self.assertFalse(executor_check.satisfied)
        self.assertEqual(executor_check.details["signals"], [])
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_missing_pytest_log_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        (self.artifact_dir / PYTEST_LAUNCH_SPEC_FILENAME).write_text(
            json.dumps({"validator_name": "pytest"}), encoding="utf-8"
        )
        self._write_advisory_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        self.assertFalse(result.mutated)
        pytest_check = self._check(result, CHECK_PYTEST_EVIDENCE)
        self.assertFalse(pytest_check.satisfied)
        self.assertIn(PYTEST_LOG_FILENAME, pytest_check.summary)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_missing_validator_launch_spec_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        (self.artifact_dir / PYTEST_LOG_FILENAME).write_text(
            PASSING_PYTEST_LOG, encoding="utf-8"
        )
        self._write_advisory_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        pytest_check = self._check(result, CHECK_PYTEST_EVIDENCE)
        self.assertFalse(pytest_check.satisfied)
        self.assertIn(PYTEST_LAUNCH_SPEC_FILENAME, pytest_check.summary)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_invalid_validator_launch_spec_json_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()
        (self.artifact_dir / PYTEST_LAUNCH_SPEC_FILENAME).write_text(
            "{not json", encoding="utf-8"
        )
        self._write_advisory_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        pytest_check = self._check(result, CHECK_PYTEST_EVIDENCE)
        self.assertFalse(pytest_check.satisfied)
        self.assertIn("could not be parsed", pytest_check.summary)

    def test_failing_pytest_summary_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence(FAILING_PYTEST_LOG)
        self._write_advisory_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        self.assertFalse(result.mutated)
        pytest_check = self._check(result, CHECK_PYTEST_EVIDENCE)
        self.assertFalse(pytest_check.satisfied)
        self.assertIn("1 failed", pytest_check.summary)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_pytest_log_without_summary_line_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence("Validator: pytest\ncollected 12 items\n")
        self._write_advisory_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        pytest_check = self._check(result, CHECK_PYTEST_EVIDENCE)
        self.assertFalse(pytest_check.satisfied)
        self.assertIn("no pytest terminal summary line", pytest_check.summary)

    def test_missing_advisory_artifact_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        self.assertFalse(result.mutated)
        advisory_check = self._check(result, CHECK_ADVISORY_EVIDENCE)
        self.assertFalse(advisory_check.satisfied)
        self.assertFalse(advisory_check.details["artifact_present"])
        self.assertIn(JSON_FILENAME, advisory_check.summary)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_malformed_advisory_artifact_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()
        self._write_advisory_evidence()
        (self.artifact_dir / JSON_FILENAME).write_text("{not json", encoding="utf-8")

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        advisory_check = self._check(result, CHECK_ADVISORY_EVIDENCE)
        self.assertFalse(advisory_check.satisfied)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_advisory_artifact_bound_to_other_task_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()
        self._write_advisory_evidence(artifact_task_key="AT-GH-158")

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        advisory_check = self._check(result, CHECK_ADVISORY_EVIDENCE)
        self.assertFalse(advisory_check.satisfied)
        self.assertIn("does not match expected task_key", advisory_check.summary)

    def test_advisory_artifact_claiming_validation_authority_blocks(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()
        self._write_advisory_evidence(
            validation_authority=True, human_review_required=False
        )

        result = self._run(confirm_transition=True)

        self.assertFalse(result.preconditions_satisfied)
        advisory_check = self._check(result, CHECK_ADVISORY_EVIDENCE)
        self.assertFalse(advisory_check.satisfied)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_all_precondition_failures_are_reported_together(self) -> None:
        self._seed_task(status="queued")

        result = self._run()

        self.assertFalse(result.preconditions_satisfied)
        self.assertEqual(len(result.blocking_errors), 4)
        self.assertFalse(result.ok)


class HappyPathTests(AdvisoryEvidenceRetryTestCase):
    def test_dry_run_reports_satisfied_without_mutating(self) -> None:
        self._seed_complete_attempt()

        result = self._run()

        self.assertTrue(result.preconditions_satisfied, result.blocking_errors)
        self.assertTrue(result.ok)
        self.assertFalse(result.mutated)
        self.assertFalse(result.audit_event_recorded)
        self.assertEqual(result.blocking_errors, ())
        self.assertTrue(result.to_dict()["dry_run"])
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)
        self.assertEqual(self._audit_events(), [])

    def test_confirmed_transition_moves_task_to_waiting_approval(self) -> None:
        self._seed_complete_attempt()

        result = self._run(confirm_transition=True)

        self.assertTrue(result.preconditions_satisfied, result.blocking_errors)
        self.assertTrue(result.mutated)
        self.assertTrue(result.ok)
        self.assertEqual(result.from_status, RETRY_FROM_STATUS)
        self.assertEqual(result.to_status, RETRY_TO_STATUS)
        self.assertEqual(result.observed_status, RETRY_TO_STATUS)
        self.assertEqual(self._current_status(), RETRY_TO_STATUS)

    def test_confirmed_transition_records_audit_event(self) -> None:
        self._seed_complete_attempt()

        result = self._run(confirm_transition=True)

        self.assertTrue(result.audit_event_recorded)
        events = self._audit_events()
        self.assertEqual(len(events), 1)
        payload = events[0]
        self.assertEqual(payload["operator"], "operator@example.com")
        self.assertEqual(payload["reason"], RETRY_REASON)
        self.assertEqual(payload["artifact_dir"], str(self.artifact_dir))
        self.assertEqual(payload["advisory_review_status"], "looks_good")
        self.assertEqual(payload["advisory_risk_level"], "low")
        self.assertEqual(payload["from_status"], RETRY_FROM_STATUS)
        self.assertEqual(payload["to_status"], RETRY_TO_STATUS)
        self.assertIn("4299 passed", payload["pytest_summary_line"])
        self.assertTrue(payload["requires_human_review"])
        self.assertTrue(payload["not_approval"])
        self.assertTrue(payload["not_merge"])
        self.assertTrue(payload["not_cleanup"])
        self.assertTrue(payload["not_validation_authority"])

        sources = {
            event.source
            for event in self.store.list_task_events(self.task_key)
            if event.event_type == "status_changed"
        }
        self.assertIn(RETRY_SOURCE, sources)

    def test_needs_attention_advisory_status_is_valid_evidence(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()
        self._write_advisory_evidence(
            review_status="needs_attention", risk_level="medium"
        )

        result = self._run(confirm_transition=True)

        self.assertTrue(result.mutated, result.blocking_errors)
        self.assertEqual(self._current_status(), RETRY_TO_STATUS)
        self.assertEqual(
            self._audit_events()[0]["advisory_review_status"], "needs_attention"
        )

    def test_no_approval_decision_is_recorded(self) -> None:
        self._seed_complete_attempt()

        self._run(confirm_transition=True)

        self.assertEqual(self.store.list_approval_decisions(self.task_key), [])

    def test_second_confirmed_run_is_rejected_after_transition(self) -> None:
        self._seed_complete_attempt()
        self._run(confirm_transition=True)

        result = self._run(confirm_transition=True)

        self.assertFalse(result.mutated)
        self.assertFalse(result.preconditions_satisfied)
        self.assertEqual(len(self._audit_events()), 1)

    def test_transition_race_raises_retry_error(self) -> None:
        self._seed_complete_attempt()

        class RacingStore:
            """Store proxy that loses the compare-and-set on the status write."""

            def __init__(self, inner: TaskMirrorStore) -> None:
                self._inner = inner

            def get_task(self, task_key: str):
                return self._inner.get_task(task_key)

            def update_task_status(self, *args, **kwargs):
                raise ValueError("Task status is 'queued'; expected 'blocked'")

            def record_task_event(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("audit event must not be recorded on a race")

        with self.assertRaises(AdvisoryEvidenceRetryError):
            run_advisory_evidence_retry(
                AdvisoryEvidenceRetryRequest(
                    task_key=self.task_key,
                    db_path=self.db_path,
                    artifact_dir=self.artifact_dir,
                    operator="operator@example.com",
                    confirm_transition=True,
                ),
                store=RacingStore(self.store),
            )

        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)
        self.assertEqual(self._audit_events(), [])


class RequestValidationTests(AdvisoryEvidenceRetryTestCase):
    def test_operator_must_not_be_empty(self) -> None:
        with self.assertRaises(ValueError):
            AdvisoryEvidenceRetryRequest(
                task_key=self.task_key,
                artifact_dir=self.artifact_dir,
                operator="   ",
            )

    def test_task_key_is_normalized(self) -> None:
        request = AdvisoryEvidenceRetryRequest(
            task_key=f"  {self.task_key}  ",
            artifact_dir=self.artifact_dir,
            operator="operator",
        )
        self.assertEqual(request.task_key, self.task_key)

    def test_confirm_transition_defaults_to_dry_run(self) -> None:
        request = AdvisoryEvidenceRetryRequest(
            task_key=self.task_key,
            artifact_dir=self.artifact_dir,
            operator="operator",
        )
        self.assertFalse(request.confirm_transition)


class PytestLogSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / PYTEST_LOG_FILENAME

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _summarize(self, text: str):
        self.log_path.write_text(text, encoding="utf-8")
        return summarize_pytest_log(self.log_path)

    def test_passing_summary_is_detected(self) -> None:
        passing, summary_line, error = self._summarize(PASSING_PYTEST_LOG)
        self.assertTrue(passing)
        self.assertIn("4299 passed", summary_line)
        self.assertIsNone(error)

    def test_failing_summary_is_detected(self) -> None:
        passing, summary_line, error = self._summarize(FAILING_PYTEST_LOG)
        self.assertFalse(passing)
        self.assertIn("1 failed", summary_line)
        self.assertIn("1 failed", error)

    def test_error_summary_is_detected(self) -> None:
        passing, _summary_line, error = self._summarize(
            "===== 2 errors, 10 passed in 3.20s =====\n"
        )
        self.assertFalse(passing)
        self.assertIn("2 errors", error)

    def test_no_tests_ran_is_not_passing(self) -> None:
        passing, _summary_line, error = self._summarize(
            "===================== no tests ran in 0.01s ====================\n"
        )
        self.assertFalse(passing)
        self.assertIsNotNone(error)

    def test_zero_passed_summary_is_not_passing(self) -> None:
        passing, _summary_line, error = self._summarize(
            "===================== 4 skipped in 0.10s =====================\n"
        )
        self.assertFalse(passing)
        self.assertIn("no passed tests", error)

    def test_missing_log_reports_error(self) -> None:
        passing, summary_line, error = summarize_pytest_log(
            self.log_path.parent / "absent.log"
        )
        self.assertFalse(passing)
        self.assertIsNone(summary_line)
        self.assertIsNotNone(error)


class CliTests(AdvisoryEvidenceRetryTestCase):
    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                exit_code = script.main(argv)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _base_args(self) -> list[str]:
        return [
            "--task-key",
            self.task_key,
            "--db-path",
            str(self.db_path),
            "--artifact-dir",
            str(self.artifact_dir),
            "--operator",
            "operator@example.com",
        ]

    def test_dry_run_prints_report_and_does_not_mutate(self) -> None:
        self._seed_complete_attempt()

        exit_code, stdout, _stderr = self._run_main(self._base_args())

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["preconditions_satisfied"])
        self.assertFalse(payload["mutated"])
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_confirm_transition_performs_transition(self) -> None:
        self._seed_complete_attempt()

        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-transition"]
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["mutated"])
        self.assertTrue(payload["audit_event_recorded"])
        self.assertEqual(payload["to_status"], RETRY_TO_STATUS)
        self.assertEqual(self._current_status(), RETRY_TO_STATUS)

    def test_confirm_transition_with_failed_precondition_exits_nonzero(self) -> None:
        self._seed_task()
        self._write_executor_evidence()
        self._write_pytest_evidence()

        exit_code, stdout, stderr = self._run_main(
            self._base_args() + ["--confirm-transition"]
        )

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout)
        self.assertFalse(payload["mutated"])
        self.assertIn("advisory", stderr.lower())
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)

    def test_empty_operator_is_rejected(self) -> None:
        self._seed_complete_attempt()

        exit_code, _stdout, _stderr = self._run_main(
            [
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--artifact-dir",
                str(self.artifact_dir),
                "--operator",
                "   ",
            ]
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(self._current_status(), RETRY_FROM_STATUS)


class RecoveryDocumentationTests(unittest.TestCase):
    def test_operator_recovery_section_is_documented(self) -> None:
        text = (REPO_ROOT / "docs" / "codex-advisory-review.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "## Operator recovery for codex_advisory_evidence blocking", text
        )
        for fragment in (
            "scripts/retry_advisory_evidence_transition.py",
            "agent_taskflow/advisory_evidence_retry.py",
            "--confirm-transition",
            "--artifact-dir",
            "reset_task_status.py",
            "Human final approval remains required",
        ):
            self.assertIn(fragment, text)


class GovernanceInvariantTests(unittest.TestCase):
    def test_module_and_cli_never_invoke_subprocesses(self) -> None:
        for source in (MODULE_SOURCE, SCRIPT_SOURCE):
            self.assertNotIn("import subprocess", source)
            self.assertNotIn("subprocess.run", source)
            self.assertNotIn("os.system", source)

    def test_module_and_cli_never_approve_merge_push_or_clean_up(self) -> None:
        for source in (MODULE_SOURCE, SCRIPT_SOURCE):
            self.assertNotIn("record_approval_decision", source)
            self.assertNotIn("git push", source)
            self.assertNotIn("gh pr merge", source)
            self.assertNotIn("worktree remove", source)
            self.assertNotIn("shutil.rmtree", source)

    def test_module_reuses_the_existing_gate_helper(self) -> None:
        self.assertIn("check_required_codex_advisory_evidence", MODULE_SOURCE)
        self.assertNotIn(
            "validate_codex_advisory_artifact_contract", MODULE_SOURCE
        )

    def test_module_only_transitions_blocked_to_waiting_approval(self) -> None:
        self.assertIn('RETRY_FROM_STATUS = "blocked"', MODULE_SOURCE)
        self.assertIn('RETRY_TO_STATUS = "waiting_approval"', MODULE_SOURCE)
        self.assertNotIn('"accepted"', MODULE_SOURCE)
        self.assertNotIn('"completed"', MODULE_SOURCE)


if __name__ == "__main__":
    unittest.main()
