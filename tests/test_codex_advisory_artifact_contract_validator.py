"""Tests for the deterministic Codex advisory artifact contract validator.

This validator validates the Codex advisory artifact *contract* only. It reads
files only. It never invokes Codex, never runs a subprocess, never validates
advisory judgment, never approves, blocks, merges, pushes, cleans up, deletes
branches/worktrees, or changes lifecycle / scheduler / runner / waiting_approval
behavior.

Crucially, the advisory statuses ``high_risk``, ``needs_attention``, and
``tool_error`` are valid Codex advisory statuses and must never fail the contract
validator by themselves.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.codex_advisory_review import (
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    REVIEWER,
    SCHEMA_VERSION,
    STDERR_FILENAME,
    STDOUT_FILENAME,
)
from agent_taskflow.codex_advisory_artifact_contract_validator import (
    VALIDATOR_NAME,
    CodexAdvisoryArtifactContractValidationRequest,
    validate_codex_advisory_artifact_contract,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_SOURCE = (
    REPO_ROOT
    / "agent_taskflow"
    / "codex_advisory_artifact_contract_validator.py"
).read_text(encoding="utf-8")
TEST_SOURCE = Path(__file__).read_text(encoding="utf-8")


class CodexAdvisoryArtifactContractValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_dir = Path(self.tmp.name) / "artifacts" / "AT-GH-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------

    def _payload(self, **overrides) -> dict:
        base = {
            "schema_version": SCHEMA_VERSION,
            "reviewer": REVIEWER,
            "task_key": "AT-GH-1",
            "review_status": "not_run",
            "risk_level": "unknown",
            "validation_authority": False,
            "human_review_required": True,
            "summary": "",
            "dry_run": True,
            "confirm_run": False,
            "codex_cli_invoked": False,
            "tool_error": None,
            "generated_at": "2026-06-18T00:00:00Z",
            "artifacts": {},
        }
        base.update(overrides)
        return base

    def _confirm_payload(self, **overrides) -> dict:
        base = self._payload(
            dry_run=False,
            confirm_run=True,
            codex_cli_invoked=True,
            review_status="looks_good",
            risk_level="low",
            artifacts={
                "codex_outputs": {
                    STDOUT_FILENAME: str(self.artifact_dir / STDOUT_FILENAME),
                    STDERR_FILENAME: str(self.artifact_dir / STDERR_FILENAME),
                }
            },
        )
        base.update(overrides)
        return base

    def _write_json(self, payload: dict) -> None:
        (self.artifact_dir / JSON_FILENAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _write_raw_json(self, text: str) -> None:
        (self.artifact_dir / JSON_FILENAME).write_text(text, encoding="utf-8")

    def _write_markdown(self) -> None:
        (self.artifact_dir / MARKDOWN_FILENAME).write_text("# md\n", encoding="utf-8")

    def _write_outputs(self) -> None:
        (self.artifact_dir / STDOUT_FILENAME).write_text("out\n", encoding="utf-8")
        (self.artifact_dir / STDERR_FILENAME).write_text("err\n", encoding="utf-8")

    def _validate(self, task_key: str | None = None):
        request = CodexAdvisoryArtifactContractValidationRequest(
            artifact_dir=self.artifact_dir,
            task_key=task_key,
        )
        return validate_codex_advisory_artifact_contract(request)

    # --- PASS cases ------------------------------------------------------

    def test_valid_dry_run_artifact_passes(self) -> None:
        self._write_json(self._payload())
        self._write_markdown()

        result = self._validate(task_key="AT-GH-1")

        self.assertTrue(result.passed, result.errors)
        self.assertTrue(result.artifact_present)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.validator_name, VALIDATOR_NAME)
        self.assertEqual(result.review_status, "not_run")
        self.assertEqual(result.risk_level, "unknown")
        self.assertIs(result.validation_authority, False)
        self.assertIs(result.human_review_required, True)

    def test_valid_confirm_run_looks_good_passes(self) -> None:
        self._write_json(self._confirm_payload(review_status="looks_good"))
        self._write_markdown()
        self._write_outputs()

        result = self._validate(task_key="AT-GH-1")

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.review_status, "looks_good")

    def test_valid_confirm_run_needs_attention_passes(self) -> None:
        self._write_json(
            self._confirm_payload(review_status="needs_attention", risk_level="medium")
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.review_status, "needs_attention")

    def test_valid_confirm_run_high_risk_passes(self) -> None:
        self._write_json(
            self._confirm_payload(review_status="high_risk", risk_level="high")
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.review_status, "high_risk")

    def test_valid_confirm_run_tool_error_passes(self) -> None:
        self._write_json(
            self._confirm_payload(
                review_status="tool_error",
                risk_level="unknown",
                tool_error={
                    "category": "codex_cli_timeout",
                    "message": "Codex CLI timed out after 300s",
                },
            )
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.review_status, "tool_error")

    def test_result_returns_normalized_artifact_paths(self) -> None:
        self._write_json(self._confirm_payload())
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertEqual(result.json_path, str(self.artifact_dir / JSON_FILENAME))
        self.assertEqual(
            result.markdown_path, str(self.artifact_dir / MARKDOWN_FILENAME)
        )
        self.assertEqual(result.stdout_path, str(self.artifact_dir / STDOUT_FILENAME))
        self.assertEqual(result.stderr_path, str(self.artifact_dir / STDERR_FILENAME))

    # --- FAIL cases ------------------------------------------------------

    def test_missing_json_fails(self) -> None:
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertFalse(result.artifact_present)
        self.assertIsNone(result.json_path)
        self.assertTrue(result.errors)

    def test_malformed_json_fails(self) -> None:
        self._write_raw_json("{not valid json")
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(result.artifact_present)
        self.assertTrue(any("could not be parsed" in e for e in result.errors))

    def test_json_array_fails(self) -> None:
        self._write_raw_json("[1, 2, 3]")
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("not a JSON object" in e for e in result.errors))

    def test_json_scalar_fails(self) -> None:
        self._write_raw_json("42")
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("not a JSON object" in e for e in result.errors))

    def test_missing_review_status_fails(self) -> None:
        payload = self._payload()
        del payload["review_status"]
        self._write_json(payload)
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("review_status" in e for e in result.errors))

    def test_invalid_review_status_fails(self) -> None:
        self._write_json(self._payload(review_status="approved"))
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("review_status" in e for e in result.errors))

    def test_missing_risk_level_fails(self) -> None:
        payload = self._payload()
        del payload["risk_level"]
        self._write_json(payload)
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("risk_level" in e for e in result.errors))

    def test_invalid_risk_level_fails(self) -> None:
        self._write_json(self._payload(risk_level="catastrophic"))
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("risk_level" in e for e in result.errors))

    def test_missing_validation_authority_fails(self) -> None:
        payload = self._payload()
        del payload["validation_authority"]
        self._write_json(payload)
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("validation_authority" in e for e in result.errors))

    def test_validation_authority_true_fails(self) -> None:
        self._write_json(self._payload(validation_authority=True))
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("validation_authority" in e for e in result.errors))

    def test_missing_human_review_required_fails(self) -> None:
        payload = self._payload()
        del payload["human_review_required"]
        self._write_json(payload)
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("human_review_required" in e for e in result.errors))

    def test_human_review_required_false_fails(self) -> None:
        self._write_json(self._payload(human_review_required=False))
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("human_review_required" in e for e in result.errors))

    def test_wrong_task_key_fails(self) -> None:
        self._write_json(self._payload(task_key="AT-GH-999"))
        self._write_markdown()

        result = self._validate(task_key="AT-GH-1")

        self.assertFalse(result.passed)
        self.assertTrue(any("task_key" in e for e in result.errors))

    def test_missing_markdown_companion_fails(self) -> None:
        self._write_json(self._payload())

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any(MARKDOWN_FILENAME in e for e in result.errors))

    def test_confirm_run_missing_stdout_stderr_fails(self) -> None:
        self._write_json(self._confirm_payload())
        self._write_markdown()
        # Intentionally do not write stdout/stderr companions.

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any(STDOUT_FILENAME in e for e in result.errors))
        self.assertTrue(any(STDERR_FILENAME in e for e in result.errors))

    def test_invalid_tool_error_structure_fails(self) -> None:
        self._write_json(
            self._confirm_payload(
                review_status="tool_error",
                tool_error={"category": "codex_cli_timeout"},  # missing message
            )
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("tool_error" in e for e in result.errors))

    def test_missing_schema_version_fails(self) -> None:
        payload = self._payload()
        del payload["schema_version"]
        self._write_json(payload)
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("schema_version" in e for e in result.errors))

    def test_wrong_reviewer_fails(self) -> None:
        self._write_json(self._payload(reviewer="totally-different"))
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("reviewer" in e for e in result.errors))

    def test_missing_task_key_field_fails(self) -> None:
        payload = self._payload()
        del payload["task_key"]
        self._write_json(payload)
        self._write_markdown()

        result = self._validate()

        self.assertFalse(result.passed)
        self.assertTrue(any("task_key" in e for e in result.errors))

    # --- non-goal semantics ----------------------------------------------

    def test_high_risk_does_not_fail_by_itself(self) -> None:
        self._write_json(
            self._confirm_payload(review_status="high_risk", risk_level="high")
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertTrue(result.passed, result.errors)

    def test_needs_attention_does_not_fail_by_itself(self) -> None:
        self._write_json(
            self._confirm_payload(review_status="needs_attention", risk_level="medium")
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertTrue(result.passed, result.errors)

    def test_tool_error_does_not_fail_when_structurally_valid(self) -> None:
        self._write_json(
            self._confirm_payload(
                review_status="tool_error",
                tool_error={"category": "codex_cli_timeout", "message": "timed out"},
            )
        )
        self._write_markdown()
        self._write_outputs()

        result = self._validate()

        self.assertTrue(result.passed, result.errors)

    def test_validator_does_not_mutate_waiting_approval_summary(self) -> None:
        # The waiting-approval summary helper must keep its lenient, never-fail
        # behavior; this strict validator must not alter that contract.
        from agent_taskflow.codex_advisory_review_summary import (
            summarize_codex_advisory_review_artifacts,
        )

        self._write_raw_json("{malformed")
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)
        validator_result = self._validate()

        # Summary stays lenient (present + warning), validator is strict (fail).
        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "malformed")
        self.assertTrue(summary.human_review_required)
        self.assertFalse(validator_result.passed)

    def test_validator_does_not_expose_ready_for_human_review(self) -> None:
        self._write_json(self._payload())
        self._write_markdown()

        result = self._validate()

        self.assertNotIn("ready_for_human_review", result.to_dict())

    def test_validator_does_not_import_scheduler_runner_modules(self) -> None:
        # The validator module must not import scheduler/runner/lifecycle modules,
        # so it cannot alter the default scheduler/runner flow.
        for forbidden in (
            "github_issue_one_task_scheduler_tick",
            "scheduler_execution_engine",
            "scheduler_confirmation",
            "scheduler_watcher",
            "dispatcher",
            "intake_runner_handoff",
            "execution_engine_contract",
            "execution_engine_manual_runtime",
        ):
            self.assertNotIn(forbidden, MODULE_SOURCE, forbidden)

    # --- source safety ---------------------------------------------------

    def test_module_does_not_import_or_call_subprocess(self) -> None:
        self.assertNotIn("import subprocess", MODULE_SOURCE)
        self.assertNotIn("subprocess.", MODULE_SOURCE)

    def test_module_has_no_lifecycle_or_authority_mutation(self) -> None:
        for forbidden in (
            "scheduler_tick",
            "scheduler.tick",
            "approved_task_runner",
            "execution_engine",
            "record_approval_decision",
            "set_task_status",
            "update_task_status",
            "git push",
            "gh pr create",
            "git merge",
            "git worktree remove",
            "git branch -d",
            "delete_worktree",
            "delete_branch",
            "cleanup",
        ):
            self.assertNotIn(forbidden, MODULE_SOURCE, forbidden)

    def test_tests_guard_against_authority_confusing_language(self) -> None:
        # The validator validates the artifact contract, not advisory judgment.
        # It must never imply Codex has validator/approval/merge authority or that
        # high_risk auto-blocks.
        for forbidden in (
            "Codex validator authority",
            "Codex approval authority",
            "auto approve",
            "auto block based on high_risk",
            "auto merge",
        ):
            self.assertNotIn(forbidden, MODULE_SOURCE, forbidden)


if __name__ == "__main__":
    unittest.main()
