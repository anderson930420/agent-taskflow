"""Tests for the v0.2.5 required Codex advisory artifact evidence gate.

These tests prove the core v0.2.5 semantic::

    Require Codex advisory evidence, not Codex approval.

A task may enter ``waiting_approval`` only when existing deterministic validators
pass AND a valid Codex advisory artifact contract is present. The gate must NOT
fail merely because Codex reported ``looks_good`` / ``needs_attention`` /
``high_risk`` / ``tool_error`` (all valid advisory statuses), but it MUST block
when the artifact is missing, malformed, or contract-invalid.

The tests cover both the pure evidence-gate helper and the actual
``run_approved_task`` transition boundary, plus governance/safety invariants.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.approved_task_runner import (
    APPROVED_TASK_STATUS,
    PHASE_CODEX_ADVISORY_EVIDENCE,
    RUN_STATUS_BLOCKED,
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.codex_advisory_evidence_gate import (
    REQUIREMENT_NAME,
    RequiredCodexAdvisoryEvidenceRequest,
    check_required_codex_advisory_evidence,
)
from agent_taskflow.codex_advisory_review import (
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    REVIEWER,
    SCHEMA_VERSION,
    STDERR_FILENAME,
    STDOUT_FILENAME,
)
from agent_taskflow.executors.manual import NoopExecutor
from agent_taskflow.models import TaskRecord
from agent_taskflow.preflight import PreflightCheck, PreflightResult
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.validators.policy import PolicyCheckValidator


REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SOURCE = (
    REPO_ROOT / "agent_taskflow" / "codex_advisory_evidence_gate.py"
).read_text(encoding="utf-8")


def _preflight_result() -> PreflightResult:
    check = PreflightCheck(
        name="python_environment",
        kind="python_runtime",
        required=True,
        status="passed",
        summary="preflight ok",
    )
    return PreflightResult(
        ok=True,
        status="passed",
        strict=False,
        executor="noop",
        validators=("policy",),
        python={"executable": "python3"},
        checks=(check,),
        missing_required=(),
        missing_optional=(),
        recommended_commands=(),
    )


def _confirm_payload(task_key: str, artifact_dir: Path, **overrides) -> dict:
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
        "generated_at": "2026-06-18T00:00:00Z",
        "artifacts": {
            "codex_outputs": {
                STDOUT_FILENAME: str(artifact_dir / STDOUT_FILENAME),
                STDERR_FILENAME: str(artifact_dir / STDERR_FILENAME),
            }
        },
    }
    base.update(overrides)
    return base


def _write_codex_artifact(
    artifact_dir: Path,
    payload: dict,
    *,
    write_markdown: bool = True,
    write_outputs: bool = True,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / JSON_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    if write_markdown:
        (artifact_dir / MARKDOWN_FILENAME).write_text(
            "# Codex Advisory Review\n", encoding="utf-8"
        )
    if write_outputs:
        (artifact_dir / STDOUT_FILENAME).write_text("out\n", encoding="utf-8")
        (artifact_dir / STDERR_FILENAME).write_text("err\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class RequiredCodexAdvisoryEvidenceHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_dir = Path(self.tmp.name) / "artifacts" / "AT-GH-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _check(self, task_key: str = "AT-GH-1"):
        return check_required_codex_advisory_evidence(
            RequiredCodexAdvisoryEvidenceRequest(
                artifact_dir=self.artifact_dir,
                task_key=task_key,
            )
        )

    def test_valid_looks_good_is_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir, _confirm_payload("AT-GH-1", self.artifact_dir)
        )
        result = self._check()
        self.assertTrue(result.satisfied, result.blocking_errors)
        self.assertEqual(result.requirement_name, REQUIREMENT_NAME)
        self.assertEqual(result.review_status, "looks_good")
        self.assertEqual(result.blocking_errors, ())

    def test_valid_needs_attention_is_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir,
            _confirm_payload(
                "AT-GH-1", self.artifact_dir,
                review_status="needs_attention", risk_level="medium",
            ),
        )
        result = self._check()
        self.assertTrue(result.satisfied, result.blocking_errors)
        self.assertEqual(result.review_status, "needs_attention")

    def test_valid_high_risk_is_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir,
            _confirm_payload(
                "AT-GH-1", self.artifact_dir,
                review_status="high_risk", risk_level="high",
            ),
        )
        result = self._check()
        self.assertTrue(result.satisfied, result.blocking_errors)
        self.assertEqual(result.review_status, "high_risk")

    def test_valid_tool_error_is_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir,
            _confirm_payload(
                "AT-GH-1", self.artifact_dir,
                review_status="tool_error",
                risk_level="unknown",
                tool_error={
                    "category": "codex_cli_timeout",
                    "message": "Codex CLI timed out after 300s",
                },
            ),
        )
        result = self._check()
        self.assertTrue(result.satisfied, result.blocking_errors)
        self.assertEqual(result.review_status, "tool_error")

    def test_missing_artifact_is_not_satisfied(self) -> None:
        result = self._check()
        self.assertFalse(result.satisfied)
        self.assertFalse(result.artifact_present)
        self.assertTrue(result.blocking_errors)
        self.assertIn("evidence is required", result.blocking_summary())

    def test_malformed_artifact_is_not_satisfied(self) -> None:
        (self.artifact_dir / JSON_FILENAME).write_text("{not json", encoding="utf-8")
        (self.artifact_dir / MARKDOWN_FILENAME).write_text("# md\n", encoding="utf-8")
        result = self._check()
        self.assertFalse(result.satisfied)
        self.assertTrue(any("could not be parsed" in e for e in result.blocking_errors))

    def test_task_key_mismatch_is_not_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir, _confirm_payload("AT-GH-999", self.artifact_dir)
        )
        result = self._check(task_key="AT-GH-1")
        self.assertFalse(result.satisfied)
        self.assertTrue(any("task_key" in e for e in result.blocking_errors))

    def test_validation_authority_true_is_not_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir,
            _confirm_payload("AT-GH-1", self.artifact_dir, validation_authority=True),
        )
        result = self._check()
        self.assertFalse(result.satisfied)
        self.assertTrue(
            any("validation_authority" in e for e in result.blocking_errors)
        )

    def test_human_review_required_false_is_not_satisfied(self) -> None:
        _write_codex_artifact(
            self.artifact_dir,
            _confirm_payload("AT-GH-1", self.artifact_dir, human_review_required=False),
        )
        result = self._check()
        self.assertFalse(result.satisfied)
        self.assertTrue(
            any("human_review_required" in e for e in result.blocking_errors)
        )

    def test_missing_is_surfaced_as_blocking_evidence_not_judgment(self) -> None:
        # A missing artifact is required-evidence-blocking, not a Codex judgment
        # failure: there is no advisory review_status to report.
        result = self._check()
        self.assertFalse(result.satisfied)
        self.assertIsNone(result.review_status)
        self.assertIn(
            "Codex advisory artifact evidence is required", result.blocking_summary()
        )


# ---------------------------------------------------------------------------
# Transition boundary regression tests (run_approved_task)
# ---------------------------------------------------------------------------


class CodexAdvisoryEvidenceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self._init_repo()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

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
        (self.repo / "README.md").write_text("agent-taskflow\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial commit")

    def _add_task(self, task_key: str) -> Path:
        artifact_dir = self.artifact_root / task_key
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Task",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return artifact_dir

    def _request(
        self, task_key: str, *, require_codex_advisory_evidence: bool = True
    ) -> ApprovedTaskRunRequest:
        return ApprovedTaskRunRequest(
            task_key=task_key,
            executor="noop",
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            worktree_root=self.repo / ".worktrees",
            base_branch="main",
            validators=("policy",),
            confirm_approved_task=True,
            preflight=True,
            require_codex_advisory_evidence=require_codex_advisory_evidence,
        )

    def _run(self, task_key: str, **kwargs):
        return run_approved_task(
            self._request(task_key, **kwargs),
            store=self.store,
            executor_registry={"noop": NoopExecutor()},
            validator_registry={"policy": PolicyCheckValidator()},
            preflight_runner=lambda **kw: _preflight_result(),
        )

    # --- valid advisory statuses reach waiting_approval ------------------

    def test_looks_good_reaches_waiting_approval(self) -> None:
        artifact_dir = self._add_task("AT-GH-501")
        _write_codex_artifact(artifact_dir, _confirm_payload("AT-GH-501", artifact_dir))

        result = self._run("AT-GH-501")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(self.store.get_task("AT-GH-501").status, APPROVED_TASK_STATUS)
        self.assertTrue(result.codex_advisory_evidence["satisfied"])
        self.assertEqual(result.codex_advisory_evidence["review_status"], "looks_good")

    def test_needs_attention_reaches_waiting_approval(self) -> None:
        artifact_dir = self._add_task("AT-GH-502")
        _write_codex_artifact(
            artifact_dir,
            _confirm_payload(
                "AT-GH-502", artifact_dir,
                review_status="needs_attention", risk_level="medium",
            ),
        )

        result = self._run("AT-GH-502")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)

    def test_high_risk_reaches_waiting_approval_and_does_not_block(self) -> None:
        artifact_dir = self._add_task("AT-GH-503")
        _write_codex_artifact(
            artifact_dir,
            _confirm_payload(
                "AT-GH-503", artifact_dir,
                review_status="high_risk", risk_level="high",
            ),
        )

        result = self._run("AT-GH-503")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(result.codex_advisory_evidence["review_status"], "high_risk")

    def test_tool_error_reaches_waiting_approval_and_does_not_block(self) -> None:
        artifact_dir = self._add_task("AT-GH-504")
        _write_codex_artifact(
            artifact_dir,
            _confirm_payload(
                "AT-GH-504", artifact_dir,
                review_status="tool_error",
                risk_level="unknown",
                tool_error={"category": "codex_cli_timeout", "message": "timed out"},
            ),
        )

        result = self._run("AT-GH-504")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(result.codex_advisory_evidence["review_status"], "tool_error")

    # --- missing / invalid evidence blocks waiting_approval --------------

    def test_missing_evidence_blocks_waiting_approval(self) -> None:
        self._add_task("AT-GH-505")  # no codex artifact written

        result = self._run("AT-GH-505")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertEqual(result.phase, PHASE_CODEX_ADVISORY_EVIDENCE)
        self.assertNotEqual(
            self.store.get_task("AT-GH-505").status, APPROVED_TASK_STATUS
        )
        self.assertIn("Codex advisory artifact evidence is required", result.error or "")
        self.assertFalse(result.codex_advisory_evidence["satisfied"])
        self.assertTrue(result.codex_advisory_evidence["blocking_errors"])

    def test_malformed_evidence_blocks_waiting_approval(self) -> None:
        artifact_dir = self._add_task("AT-GH-506")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / JSON_FILENAME).write_text("{nope", encoding="utf-8")
        (artifact_dir / MARKDOWN_FILENAME).write_text("# md\n", encoding="utf-8")

        result = self._run("AT-GH-506")

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, PHASE_CODEX_ADVISORY_EVIDENCE)
        self.assertNotEqual(
            self.store.get_task("AT-GH-506").status, APPROVED_TASK_STATUS
        )

    def test_invalid_contract_validation_authority_blocks(self) -> None:
        artifact_dir = self._add_task("AT-GH-507")
        _write_codex_artifact(
            artifact_dir,
            _confirm_payload("AT-GH-507", artifact_dir, validation_authority=True),
        )

        result = self._run("AT-GH-507")

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, PHASE_CODEX_ADVISORY_EVIDENCE)
        self.assertTrue(
            any(
                "validation_authority" in e
                for e in result.codex_advisory_evidence["blocking_errors"]
            )
        )

    def test_human_review_required_false_blocks(self) -> None:
        artifact_dir = self._add_task("AT-GH-508")
        _write_codex_artifact(
            artifact_dir,
            _confirm_payload("AT-GH-508", artifact_dir, human_review_required=False),
        )

        result = self._run("AT-GH-508")

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, PHASE_CODEX_ADVISORY_EVIDENCE)

    def test_task_key_mismatch_blocks(self) -> None:
        artifact_dir = self._add_task("AT-GH-509")
        _write_codex_artifact(artifact_dir, _confirm_payload("AT-GH-000", artifact_dir))

        result = self._run("AT-GH-509")

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, PHASE_CODEX_ADVISORY_EVIDENCE)
        self.assertTrue(
            any(
                "task_key" in e
                for e in result.codex_advisory_evidence["blocking_errors"]
            )
        )

    def test_deterministic_validator_failure_blocks_before_gate(self) -> None:
        # When a deterministic validator fails, the run blocks at validation and
        # never reaches the codex advisory evidence gate even with valid evidence.
        artifact_dir = self._add_task("AT-GH-510")
        _write_codex_artifact(artifact_dir, _confirm_payload("AT-GH-510", artifact_dir))

        from agent_taskflow.validators.base import (
            Validator,
            ValidatorContext,
            ValidatorResult,
        )

        class FailingValidator(Validator):
            name = "policy"

            def run(self, context: ValidatorContext) -> ValidatorResult:
                return ValidatorResult(
                    validator="policy",
                    status="failed",
                    exit_code=1,
                    summary="validator failed",
                )

        result = run_approved_task(
            self._request("AT-GH-510"),
            store=self.store,
            executor_registry={"noop": NoopExecutor()},
            validator_registry={"policy": FailingValidator()},
            preflight_runner=lambda **kw: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "validation")
        self.assertNotEqual(result.phase, PHASE_CODEX_ADVISORY_EVIDENCE)
        self.assertEqual(result.codex_advisory_evidence, {})

    def test_disabling_requirement_allows_waiting_approval_without_evidence(self) -> None:
        # Proves the gate is what enforces the requirement: with the requirement
        # explicitly disabled, a missing artifact no longer blocks. The default
        # (enabled) behavior is exercised by the blocking tests above.
        self._add_task("AT-GH-511")

        result = self._run("AT-GH-511", require_codex_advisory_evidence=False)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(result.codex_advisory_evidence, {})

    def test_requirement_is_enabled_by_default(self) -> None:
        request = ApprovedTaskRunRequest(
            task_key="AT-GH-512",
            executor="noop",
            repo_path=self.repo,
        )
        self.assertTrue(request.require_codex_advisory_evidence)

    def test_waiting_approval_summary_still_displays_codex_review(self) -> None:
        # v0.2.3 waiting-approval summary still surfaces the Codex advisory review
        # once the task has reached waiting_approval through the gate.
        from agent_taskflow.waiting_approval_summary import (
            WaitingApprovalSummaryRequest,
            summarize_waiting_approval_task,
        )

        artifact_dir = self._add_task("AT-GH-513")
        _write_codex_artifact(
            artifact_dir,
            _confirm_payload(
                "AT-GH-513", artifact_dir,
                review_status="needs_attention", risk_level="medium",
            ),
        )

        run_result = self._run("AT-GH-513")
        self.assertTrue(run_result.ok, run_result.error)

        summary = summarize_waiting_approval_task(
            WaitingApprovalSummaryRequest(
                task_key="AT-GH-513",
                db_path=self.db_path,
                artifact_root=self.artifact_root,
            )
        )
        self.assertTrue(summary.codex_advisory_review["present"])
        self.assertEqual(
            summary.codex_advisory_review["review_status"], "needs_attention"
        )
        # The advisory content never grants approval; human review remains required.
        self.assertFalse(summary.safety["approved"])

    def test_blocked_run_does_not_push_pr_merge_cleanup_or_approve(self) -> None:
        self._add_task("AT-GH-514")  # missing evidence -> blocked at gate

        result = self._run("AT-GH-514")

        self.assertFalse(result.ok)
        for flag in (
            "branch_pushed",
            "pr_created",
            "merged",
            "approved",
            "cleanup_performed",
        ):
            self.assertFalse(result.safety[flag], flag)
        # Human approval is never auto-confirmed by the gate.
        self.assertTrue(result.safety["human_approval_required"])


# ---------------------------------------------------------------------------
# Governance / safety source invariants
# ---------------------------------------------------------------------------


class CodexAdvisoryEvidenceGateSafetyTests(unittest.TestCase):
    def test_gate_does_not_import_or_call_subprocess(self) -> None:
        self.assertNotIn("import subprocess", GATE_SOURCE)
        self.assertNotIn("subprocess.", GATE_SOURCE)

    def test_gate_does_not_invoke_codex_cli(self) -> None:
        for forbidden in ("generate_codex_advisory_review", "invoke_codex_cli", "codex_command"):
            self.assertNotIn(forbidden, GATE_SOURCE, forbidden)

    def test_gate_does_not_perform_publish_or_cleanup_actions(self) -> None:
        for forbidden in (
            "git push",
            "gh pr create",
            "gh pr merge",
            "git merge",
            "delete_branch",
            "delete_worktree",
            "git worktree remove",
            "git branch -d",
            "cleanup",
        ):
            self.assertNotIn(forbidden, GATE_SOURCE, forbidden)

    def test_gate_does_not_change_authority_or_lifecycle(self) -> None:
        # The gate reports required-evidence status only; it must not mutate task
        # lifecycle, approval records, or claim validation/execution authority.
        for forbidden in (
            "update_task_status",
            "record_approval_decision",
            "set_task_status",
            "ExecutionEngine",
        ):
            self.assertNotIn(forbidden, GATE_SOURCE, forbidden)

    def test_gate_does_not_import_scheduler_runner_modules(self) -> None:
        for forbidden in (
            "approved_task_runner",
            "github_issue_one_task_scheduler_tick",
            "scheduler_execution_engine",
            "scheduler_watcher",
            "dispatcher",
            "execution_engine",
        ):
            self.assertNotIn(forbidden, GATE_SOURCE, forbidden)


if __name__ == "__main__":
    unittest.main()
