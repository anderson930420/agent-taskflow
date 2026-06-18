"""Tests for the read-only Codex advisory review artifact summary.

This summary is human-review evidence only. It never invokes Codex, never runs a
subprocess, never validates, approves, blocks, merges, pushes, cleans up,
deletes branches/worktrees, or changes lifecycle.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow import codex_advisory_review as contract
from agent_taskflow.codex_advisory_review import (
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    STDERR_FILENAME,
    STDOUT_FILENAME,
    CodexAdvisoryReviewRequest,
    generate_codex_advisory_review,
)
from agent_taskflow.codex_advisory_review_summary import (
    CodexAdvisoryReviewSummary,
    summarize_codex_advisory_review_artifacts,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_SOURCE = (
    REPO_ROOT / "agent_taskflow" / "codex_advisory_review_summary.py"
).read_text(encoding="utf-8")


class CodexAdvisoryReviewSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_dir = Path(self.tmp.name) / "artifacts" / "AT-GH-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _payload(self, **overrides) -> dict:
        base = {
            "schema_version": "codex_advisory_review.v1",
            "reviewer": "codex-cli",
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
            "artifacts": {},
        }
        base.update(overrides)
        return base

    def _write_json(self, payload: dict) -> None:
        (self.artifact_dir / JSON_FILENAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _write_markdown(self) -> None:
        (self.artifact_dir / MARKDOWN_FILENAME).write_text("# md\n", encoding="utf-8")

    def _write_outputs(self) -> None:
        (self.artifact_dir / STDOUT_FILENAME).write_text("out\n", encoding="utf-8")
        (self.artifact_dir / STDERR_FILENAME).write_text("err\n", encoding="utf-8")

    # --- Detection -------------------------------------------------------

    def test_no_artifacts_reports_absent(self) -> None:
        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertFalse(summary.present)
        self.assertEqual(summary.review_status, "missing")
        self.assertEqual(summary.risk_level, "unknown")
        self.assertFalse(summary.validation_authority)
        self.assertTrue(summary.human_review_required)
        self.assertIsNone(summary.json_path)
        self.assertEqual(summary.warnings, ())

    def test_none_artifact_dir_reports_absent(self) -> None:
        summary = summarize_codex_advisory_review_artifacts(None)

        self.assertFalse(summary.present)
        self.assertEqual(summary.review_status, "missing")
        self.assertEqual(summary.warnings, ())

    def test_valid_dry_run_artifact(self) -> None:
        self._write_json(self._payload())
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "not_run")
        self.assertEqual(summary.risk_level, "unknown")
        self.assertFalse(summary.validation_authority)
        self.assertTrue(summary.human_review_required)
        self.assertIsNotNone(summary.json_path)
        self.assertIsNotNone(summary.markdown_path)
        self.assertEqual(summary.warnings, ())

    def test_generated_dry_run_artifact_is_surfaced(self) -> None:
        generate_codex_advisory_review(
            CodexAdvisoryReviewRequest(
                task_key="AT-GH-1", artifact_dir=self.artifact_dir
            )
        )

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "not_run")
        self.assertEqual(summary.risk_level, "unknown")
        self.assertEqual(summary.warnings, ())

    def test_confirm_run_looks_good_is_surfaced(self) -> None:
        self._write_json(
            self._payload(
                review_status="looks_good",
                risk_level="low",
                dry_run=False,
                confirm_run=True,
                codex_cli_invoked=True,
                summary="No concerns",
            )
        )
        self._write_markdown()
        self._write_outputs()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "looks_good")
        self.assertEqual(summary.risk_level, "low")
        self.assertEqual(summary.summary, "No concerns")
        self.assertIsNotNone(summary.stdout_path)
        self.assertIsNotNone(summary.stderr_path)
        self.assertEqual(summary.warnings, ())

    def test_confirm_run_needs_attention_is_surfaced(self) -> None:
        self._write_json(
            self._payload(
                review_status="needs_attention",
                risk_level="medium",
                dry_run=False,
                confirm_run=True,
                codex_cli_invoked=True,
            )
        )
        self._write_markdown()
        self._write_outputs()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "needs_attention")
        self.assertEqual(summary.risk_level, "medium")
        self.assertTrue(summary.human_review_required)
        self.assertEqual(summary.warnings, ())

    def test_confirm_run_high_risk_does_not_block(self) -> None:
        self._write_json(
            self._payload(
                review_status="high_risk",
                risk_level="high",
                dry_run=False,
                confirm_run=True,
                codex_cli_invoked=True,
            )
        )
        self._write_markdown()
        self._write_outputs()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "high_risk")
        self.assertEqual(summary.risk_level, "high")
        # Advisory only: still false / true, never a gate decision.
        self.assertFalse(summary.validation_authority)
        self.assertTrue(summary.human_review_required)
        self.assertEqual(summary.warnings, ())

    def test_confirm_run_tool_error_does_not_fail_summary(self) -> None:
        tool_error = {"category": "codex_cli_timeout", "message": "timed out"}
        self._write_json(
            self._payload(
                review_status="tool_error",
                risk_level="unknown",
                dry_run=False,
                confirm_run=True,
                codex_cli_invoked=True,
                tool_error=tool_error,
            )
        )
        self._write_markdown()
        self._write_outputs()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "tool_error")
        self.assertEqual(summary.tool_error, tool_error)
        self.assertEqual(summary.warnings, ())

    # --- Safety invariants ----------------------------------------------

    def test_validation_authority_true_is_not_trusted(self) -> None:
        self._write_json(self._payload(validation_authority=True))
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertFalse(summary.validation_authority)
        self.assertTrue(
            any("validation_authority" in warning for warning in summary.warnings)
        )

    def test_human_review_required_false_is_not_trusted(self) -> None:
        self._write_json(self._payload(human_review_required=False))
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertTrue(summary.human_review_required)
        self.assertTrue(
            any("human_review_required" in warning for warning in summary.warnings)
        )

    def test_invalid_review_status_becomes_unknown(self) -> None:
        self._write_json(self._payload(review_status="approved"))
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertEqual(summary.review_status, "unknown")
        self.assertTrue(
            any("review_status" in warning for warning in summary.warnings)
        )

    def test_invalid_risk_level_becomes_unknown(self) -> None:
        self._write_json(self._payload(risk_level="critical"))
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertEqual(summary.risk_level, "unknown")
        self.assertTrue(any("risk_level" in warning for warning in summary.warnings))

    def test_malformed_json_does_not_fail_summary(self) -> None:
        (self.artifact_dir / JSON_FILENAME).write_text(
            "{ not valid json", encoding="utf-8"
        )
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "malformed")
        self.assertEqual(summary.risk_level, "unknown")
        self.assertFalse(summary.validation_authority)
        self.assertTrue(summary.human_review_required)
        self.assertTrue(len(summary.warnings) >= 1)

    def test_non_object_json_is_malformed(self) -> None:
        (self.artifact_dir / JSON_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "malformed")
        self.assertTrue(len(summary.warnings) >= 1)

    # --- Companion files -------------------------------------------------

    def test_markdown_path_present_when_markdown_exists(self) -> None:
        self._write_json(self._payload())
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertEqual(
            summary.markdown_path, str(self.artifact_dir / MARKDOWN_FILENAME)
        )

    def test_stdout_stderr_paths_present_for_confirm_run(self) -> None:
        self._write_json(
            self._payload(
                review_status="looks_good",
                dry_run=False,
                confirm_run=True,
                codex_cli_invoked=True,
            )
        )
        self._write_markdown()
        self._write_outputs()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertEqual(
            summary.stdout_path, str(self.artifact_dir / STDOUT_FILENAME)
        )
        self.assertEqual(
            summary.stderr_path, str(self.artifact_dir / STDERR_FILENAME)
        )

    def test_missing_companion_files_warn_without_failing(self) -> None:
        # Confirm-run JSON references stdout/stderr outputs, and markdown is
        # always generated, but none of the companion files exist on disk.
        self._write_json(
            self._payload(
                review_status="looks_good",
                dry_run=False,
                confirm_run=True,
                codex_cli_invoked=True,
            )
        )

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)

        self.assertTrue(summary.present)
        self.assertEqual(summary.review_status, "looks_good")
        self.assertIsNone(summary.markdown_path)
        self.assertIsNone(summary.stdout_path)
        self.assertIsNone(summary.stderr_path)
        warning_text = " ".join(summary.warnings)
        self.assertIn(MARKDOWN_FILENAME, warning_text)
        self.assertIn(STDOUT_FILENAME, warning_text)
        self.assertIn(STDERR_FILENAME, warning_text)

    # --- Source safety ---------------------------------------------------

    def test_module_does_not_import_or_call_subprocess(self) -> None:
        self.assertNotIn("import subprocess", MODULE_SOURCE)
        self.assertNotIn("subprocess.", MODULE_SOURCE)

    def test_module_has_no_lifecycle_or_authority_mutation(self) -> None:
        for forbidden in (
            "scheduler_tick",
            "scheduler.tick",
            "approved_task_runner",
            "ExecutionEngine",
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
            "cleanup",
        ):
            self.assertNotIn(forbidden, MODULE_SOURCE, forbidden)

    def test_summarizing_never_invokes_codex_subprocess(self) -> None:
        self._write_json(self._payload())
        self._write_markdown()

        with mock.patch.object(contract.subprocess, "run") as run_mock:
            summarize_codex_advisory_review_artifacts(self.artifact_dir)

        run_mock.assert_not_called()

    def test_summary_never_uses_ambiguous_authority_language(self) -> None:
        # The summary must never imply approve/block/validator pass-fail/merge
        # authority. review_status is advisory-only and the two hard invariants
        # are always enforced across every artifact state.
        ambiguous = {"approve", "approved", "block", "blocked", "merge-ready", "merge_ready"}
        cases = [
            self._payload(),
            self._payload(review_status="high_risk", risk_level="high"),
            self._payload(review_status="approved"),  # invalid -> unknown
        ]
        for payload in cases:
            self._write_json(payload)
            self._write_markdown()
            summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)
            self.assertNotIn(summary.review_status, ambiguous)
            self.assertFalse(summary.validation_authority)
            self.assertTrue(summary.human_review_required)
            self.assertNotIn("validation_authority=true", MODULE_SOURCE)

    def test_to_dict_exposes_expected_keys(self) -> None:
        self._write_json(self._payload())
        self._write_markdown()

        summary = summarize_codex_advisory_review_artifacts(self.artifact_dir)
        data = summary.to_dict()

        self.assertEqual(
            set(data),
            {
                "present",
                "review_status",
                "risk_level",
                "validation_authority",
                "human_review_required",
                "json_path",
                "markdown_path",
                "stdout_path",
                "stderr_path",
                "summary",
                "tool_error",
                "review_checklist",
                "human_review_priorities",
                "warnings",
            },
        )
        self.assertIsInstance(data["warnings"], list)
        self.assertIsInstance(summary, CodexAdvisoryReviewSummary)


if __name__ == "__main__":
    unittest.main()
