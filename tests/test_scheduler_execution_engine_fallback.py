"""Tests for the P5-e scheduler ExecutionEngine fallback hardening layer.

The fallback layer is a pure, behavior-free classification: it inspects a
legacy scheduler tick payload and the P5-d ``execution_engine`` opt-in evidence
block and reports whether fallback to the legacy scheduler path is required and
why. These tests assert the classification rules, the pinned authority
semantics (``effective_authority="legacy_scheduler"``,
``engine_authority=False``, ``engine_result_accepted_as_authority=False``), and
that the layer never executes, wires, or touches any runtime path or the
filesystem.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import agent_taskflow.scheduler_execution_engine_fallback as fallback_module
from agent_taskflow.scheduler_execution_engine_fallback import (
    EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER,
    SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION,
    SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE,
    SchedulerExecutionEngineFallbackAssessment,
    SchedulerExecutionEngineFallbackAssessmentInput,
    assess_scheduler_execution_engine_fallback,
    scheduler_execution_engine_fallback_assessment_to_json_dict,
)


TASK_KEY = "AT-P5E"

UNSAFE_SAFETY_MARKERS = (
    "approval_authority",
    "approved",
    "merged",
    "github_mutated",
    "branch_pushed",
    "draft_pr_created",
    "cleanup_performed",
    "archived",
    "closed_out",
    "branch_deleted",
    "worktree_deleted",
    "daemon_started",
    "webhook_started",
    "background_worker_started",
    "scheduler_loop_started",
    "multi_task_batch_started",
)


def make_legacy(**overrides: Any) -> dict[str, Any]:
    """Return a legacy scheduler tick payload (the effective authority)."""

    legacy: dict[str, Any] = {
        "ok": True,
        "status": "execution_completed",
        "mode": "confirmed",
        "repo": "anderson930420/agent-taskflow",
        "selected_task_key": TASK_KEY,
    }
    legacy.update(overrides)
    return legacy


def make_safety(**overrides: Any) -> dict[str, Any]:
    safety: dict[str, Any] = {marker: False for marker in UNSAFE_SAFETY_MARKERS}
    safety.update(
        {
            "scheduler_tick": True,
            "one_task_only": True,
            "execution_only": True,
            "publish_after_execution": False,
            "human_review_required": True,
        }
    )
    safety.update(overrides)
    return safety


def make_evidence(**overrides: Any) -> dict[str, Any]:
    """Return a clean P5-d ``execution_engine`` opt-in evidence block."""

    evidence: dict[str, Any] = {
        "schema_version": "scheduler_execution_engine_opt_in_path.v1",
        "source": "scheduler_execution_engine_opt_in_path",
        "enabled": True,
        "executed": True,
        "confirmed_mode_only": True,
        "mode": "execution_only",
        "engine": "RecordingEngine",
        "engine_invocation_count": 1,
        "ok": True,
        "status": "waiting_approval",
        "selected_task_key": TASK_KEY,
        "request_summary": {
            "task_key": TASK_KEY,
            "source": "scheduled_tick",
            "publish_after_execution": False,
            "mode": "execution_only",
            "execution_only": True,
            "one_task_only": True,
            "scheduler_tick": True,
        },
        "request": {
            "task_key": TASK_KEY,
            "source": "scheduled_tick",
            "metadata": {
                "publish_after_execution": False,
                "mode": "execution_only",
                "execution_only": True,
                "one_task_only": True,
                "scheduler_tick": True,
            },
        },
        "shadow_compare": {"matched": True, "mismatches": [], "warnings": []},
        "safety": make_safety(),
    }
    evidence.update(overrides)
    return evidence


def assess(
    *,
    legacy: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None | object = "default",
    metadata: dict[str, Any] | None = None,
) -> SchedulerExecutionEngineFallbackAssessment:
    return assess_scheduler_execution_engine_fallback(
        SchedulerExecutionEngineFallbackAssessmentInput(
            legacy_tick_payload=make_legacy() if legacy is None else legacy,
            execution_engine_evidence=(
                make_evidence() if evidence == "default" else evidence  # type: ignore[arg-type]
            ),
            metadata={} if metadata is None else metadata,
        )
    )


class CleanCandidateTests(unittest.TestCase):
    def test_clean_evidence_requires_no_fallback_but_grants_no_authority(
        self,
    ) -> None:
        result = assess()

        self.assertIsInstance(
            result, SchedulerExecutionEngineFallbackAssessment
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.fallback_required)
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(result.fallback_reasons, ())
        self.assertTrue(result.engine_candidate_usable_for_future_migration)
        # The candidate is usable for *future* migration only: it carries no
        # authority in P5-e.
        self.assertEqual(
            result.effective_authority, EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER
        )
        self.assertEqual(result.effective_authority, "legacy_scheduler")
        self.assertFalse(result.engine_authority)
        self.assertFalse(result.engine_result_accepted_as_authority)
        self.assertTrue(result.legacy_ok_preserved)
        self.assertTrue(result.legacy_status_preserved)
        self.assertTrue(result.publication_boundary_preserved)
        self.assertTrue(result.safety_boundary_preserved)
        self.assertEqual(
            result.schema_version,
            SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.source, SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE
        )

    def test_summary_records_legacy_and_engine_decision_fields(self) -> None:
        result = assess()

        summary = result.summary
        self.assertIs(summary["legacy_ok"], True)
        self.assertEqual(summary["legacy_status"], "execution_completed")
        self.assertIs(summary["engine_ok"], True)
        self.assertEqual(summary["engine_status"], "waiting_approval")
        self.assertEqual(summary["effective_authority"], "legacy_scheduler")
        self.assertIs(summary["engine_authority"], False)
        self.assertIs(summary["engine_result_accepted_as_authority"], False)
        self.assertIs(summary["fallback_required"], False)
        self.assertEqual(summary["fallback_reasons"], [])

    def test_legacy_failure_is_recorded_without_being_overridden(self) -> None:
        # A failed legacy tick stays the effective authority: the assessment
        # records the legacy decision as-is and never replaces it.
        result = assess(legacy=make_legacy(ok=False, status="automation_error"))

        self.assertIs(result.summary["legacy_ok"], False)
        self.assertEqual(result.summary["legacy_status"], "automation_error")
        self.assertEqual(result.effective_authority, "legacy_scheduler")
        self.assertFalse(result.engine_authority)


class FallbackClassificationTests(unittest.TestCase):
    def test_absent_engine_evidence_requires_fallback(self) -> None:
        result = assess(evidence=None)

        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "engine_evidence_absent")
        self.assertIn("engine_evidence_absent", result.fallback_reasons)
        self.assertFalse(result.engine_candidate_usable_for_future_migration)
        self.assertFalse(result.engine_authority)
        self.assertEqual(result.effective_authority, "legacy_scheduler")
        self.assertFalse(result.summary["engine_evidence_present"])
        self.assertIsNone(result.summary["engine_ok"])
        self.assertIsNone(result.summary["engine_status"])
        # The engine never ran, so nothing crossed the boundaries.
        self.assertTrue(result.publication_boundary_preserved)
        self.assertTrue(result.safety_boundary_preserved)

    def test_disabled_engine_evidence_requires_fallback(self) -> None:
        result = assess(evidence=make_evidence(enabled=False))

        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "engine_not_enabled")
        self.assertFalse(result.engine_candidate_usable_for_future_migration)

    def test_not_executed_engine_evidence_requires_fallback(self) -> None:
        result = assess(evidence=make_evidence(executed=False))

        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "engine_not_executed")
        self.assertFalse(result.engine_candidate_usable_for_future_migration)

    def test_engine_ok_false_requires_fallback(self) -> None:
        result = assess(evidence=make_evidence(ok=False))

        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "engine_not_ok")
        self.assertFalse(result.engine_candidate_usable_for_future_migration)

    def test_engine_error_status_requires_fallback(self) -> None:
        result = assess(evidence=make_evidence(status="engine_error"))

        self.assertTrue(result.fallback_required)
        self.assertEqual(
            result.fallback_reason, "engine_failure_status:engine_error"
        )
        self.assertFalse(result.engine_candidate_usable_for_future_migration)

    def test_failed_blocked_and_not_executed_statuses_require_fallback(
        self,
    ) -> None:
        for status in (
            "validator_failed",
            "executor_failed",
            "preflight_failed",
            "blocked",
            "not_executed",
        ):
            with self.subTest(status=status):
                result = assess(evidence=make_evidence(status=status))
                self.assertTrue(result.fallback_required, msg=status)
                self.assertIn(
                    f"engine_failure_status:{status}",
                    result.fallback_reasons,
                    msg=status,
                )
                self.assertFalse(
                    result.engine_candidate_usable_for_future_migration,
                    msg=status,
                )

    def test_clean_statuses_do_not_require_fallback(self) -> None:
        for status in ("waiting_approval", "execution_completed"):
            with self.subTest(status=status):
                result = assess(evidence=make_evidence(status=status))
                self.assertFalse(result.fallback_required, msg=status)

    def test_shadow_compare_mismatch_requires_fallback(self) -> None:
        mismatches = [
            "task_key mismatch: legacy 'AT-OTHER' != engine 'AT-P5E'",
            "repo/project mismatch: legacy 'a/b' != engine 'c/d'",
        ]
        result = assess(
            evidence=make_evidence(
                shadow_compare={"matched": False, "mismatches": mismatches}
            )
        )

        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "shadow_compare_mismatch")
        self.assertFalse(result.engine_candidate_usable_for_future_migration)
        shadow_summary = result.summary["shadow_compare"]
        self.assertTrue(shadow_summary["present"])
        self.assertIs(shadow_summary["matched"], False)
        self.assertEqual(shadow_summary["mismatch_count"], 2)
        self.assertEqual(shadow_summary["mismatches"], mismatches)

    def test_missing_shadow_compare_requires_fallback(self) -> None:
        result = assess(evidence=make_evidence(shadow_compare=None))

        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "shadow_compare_missing")
        self.assertFalse(result.summary["shadow_compare"]["present"])

    def test_each_unsafe_safety_marker_requires_fallback(self) -> None:
        for marker in UNSAFE_SAFETY_MARKERS:
            with self.subTest(marker=marker):
                result = assess(
                    evidence=make_evidence(safety=make_safety(**{marker: True}))
                )
                self.assertTrue(result.fallback_required, msg=marker)
                self.assertEqual(
                    result.fallback_reason,
                    "unsafe_engine_safety_marker",
                    msg=marker,
                )
                self.assertFalse(result.safety_boundary_preserved, msg=marker)
                self.assertFalse(
                    result.engine_candidate_usable_for_future_migration,
                    msg=marker,
                )
                self.assertIn(
                    marker, result.summary["unsafe_safety_markers"], msg=marker
                )

    def test_missing_safety_block_requires_fallback(self) -> None:
        result = assess(evidence=make_evidence(safety=None))

        self.assertTrue(result.fallback_required)
        self.assertEqual(result.fallback_reason, "engine_safety_block_missing")
        self.assertFalse(result.safety_boundary_preserved)

    def test_publication_boundary_violations_require_fallback(self) -> None:
        violations: dict[str, dict[str, Any]] = {
            "publish_after_execution_true": {"publish_after_execution": True},
            "mode_not_execution_only": {"mode": "publication"},
            "execution_only_false": {"execution_only": False},
        }
        for label, override in violations.items():
            with self.subTest(label=label):
                evidence = make_evidence()
                evidence["request_summary"].update(override)
                evidence["request"]["metadata"].update(override)
                result = assess(evidence=evidence)
                self.assertTrue(result.fallback_required, msg=label)
                self.assertIn(
                    "publication_boundary_violation",
                    result.fallback_reasons,
                    msg=label,
                )
                self.assertFalse(
                    result.publication_boundary_preserved, msg=label
                )
                self.assertFalse(
                    result.engine_candidate_usable_for_future_migration,
                    msg=label,
                )

    def test_publication_markers_fall_back_to_request_metadata(self) -> None:
        evidence = make_evidence(request_summary=None)
        result = assess(evidence=evidence)

        self.assertFalse(result.fallback_required)
        self.assertTrue(result.publication_boundary_preserved)
        boundary = result.summary["publication_boundary"]
        self.assertIs(boundary["publish_after_execution"], False)
        self.assertEqual(boundary["mode"], "execution_only")
        self.assertIs(boundary["execution_only"], True)

    def test_executed_engine_without_publication_markers_is_violation(
        self,
    ) -> None:
        evidence = make_evidence(request_summary=None, request=None)
        result = assess(evidence=evidence)

        self.assertTrue(result.fallback_required)
        self.assertIn(
            "publication_boundary_violation", result.fallback_reasons
        )
        self.assertFalse(result.publication_boundary_preserved)

    def test_legacy_payload_missing_ok_requires_fallback(self) -> None:
        legacy = make_legacy()
        del legacy["ok"]
        result = assess(legacy=legacy)

        self.assertTrue(result.fallback_required)
        self.assertIn("legacy_ok_missing", result.fallback_reasons)
        self.assertFalse(result.legacy_ok_preserved)

    def test_legacy_payload_missing_status_requires_fallback(self) -> None:
        legacy = make_legacy()
        del legacy["status"]
        result = assess(legacy=legacy)

        self.assertTrue(result.fallback_required)
        self.assertIn("legacy_status_missing", result.fallback_reasons)
        self.assertFalse(result.legacy_status_preserved)

    def test_compound_failure_collects_all_reasons(self) -> None:
        result = assess(
            evidence=make_evidence(
                enabled=False,
                executed=False,
                ok=False,
                status="engine_error",
            )
        )

        self.assertTrue(result.fallback_required)
        # The primary reason is the first failed check, in rule order.
        self.assertEqual(result.fallback_reason, "engine_not_enabled")
        for reason in (
            "engine_not_enabled",
            "engine_not_executed",
            "engine_not_ok",
            "engine_failure_status:engine_error",
        ):
            self.assertIn(reason, result.fallback_reasons, msg=reason)


class InputValidationTests(unittest.TestCase):
    def test_non_mapping_legacy_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            TypeError, "legacy_tick_payload must be a mapping"
        ):
            SchedulerExecutionEngineFallbackAssessmentInput(
                legacy_tick_payload=["not", "a", "mapping"],  # type: ignore[arg-type]
                execution_engine_evidence=make_evidence(),
            )

    def test_non_mapping_evidence_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            TypeError, "execution_engine_evidence must be a mapping or None"
        ):
            SchedulerExecutionEngineFallbackAssessmentInput(
                legacy_tick_payload=make_legacy(),
                execution_engine_evidence="not-a-mapping",  # type: ignore[arg-type]
            )

    def test_non_mapping_metadata_is_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "metadata must be a mapping"):
            SchedulerExecutionEngineFallbackAssessmentInput(
                legacy_tick_payload=make_legacy(),
                execution_engine_evidence=make_evidence(),
                metadata=["nope"],  # type: ignore[arg-type]
            )

    def test_input_mappings_are_copied_defensively(self) -> None:
        legacy = make_legacy()
        evidence = make_evidence()
        metadata: dict[str, Any] = {"tick_id": "tick-7", "labels": ["one"]}
        build_input = SchedulerExecutionEngineFallbackAssessmentInput(
            legacy_tick_payload=legacy,
            execution_engine_evidence=evidence,
            metadata=metadata,
        )

        legacy["ok"] = False
        legacy["status"] = "mutated"
        evidence["ok"] = False
        evidence["safety"]["approved"] = True
        metadata["tick_id"] = "mutated"
        metadata["labels"].append("mutated")

        self.assertIs(build_input.legacy_tick_payload["ok"], True)
        self.assertEqual(
            build_input.legacy_tick_payload["status"], "execution_completed"
        )
        assert build_input.execution_engine_evidence is not None
        self.assertIs(build_input.execution_engine_evidence["ok"], True)
        self.assertIs(
            build_input.execution_engine_evidence["safety"]["approved"], False
        )
        self.assertEqual(build_input.metadata["tick_id"], "tick-7")
        self.assertEqual(build_input.metadata["labels"], ["one"])

    def test_mutation_after_assessment_does_not_mutate_result(self) -> None:
        legacy = make_legacy()
        evidence = make_evidence()
        build_input = SchedulerExecutionEngineFallbackAssessmentInput(
            legacy_tick_payload=legacy,
            execution_engine_evidence=evidence,
        )
        result = assess_scheduler_execution_engine_fallback(build_input)

        legacy["status"] = "mutated"
        evidence["safety"]["merged"] = True
        evidence["shadow_compare"]["matched"] = False

        self.assertEqual(result.summary["legacy_status"], "execution_completed")
        self.assertFalse(result.fallback_required)
        self.assertTrue(result.safety_boundary_preserved)


class JsonHelperTests(unittest.TestCase):
    def test_assessment_serializes_to_json_compatible_dict(self) -> None:
        result = assess()

        payload = scheduler_execution_engine_fallback_assessment_to_json_dict(
            result
        )

        self.assertIsInstance(payload, dict)
        # json.dumps raises if anything is not JSON-compatible.
        json.dumps(payload)
        self.assertEqual(
            payload["schema_version"],
            SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION,
        )
        self.assertEqual(
            payload["source"], SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE
        )
        self.assertEqual(payload["effective_authority"], "legacy_scheduler")
        self.assertIs(payload["engine_authority"], False)
        self.assertIs(payload["engine_result_accepted_as_authority"], False)
        self.assertIs(payload["fallback_required"], False)
        self.assertIsNone(payload["fallback_reason"])
        self.assertEqual(payload["fallback_reasons"], [])
        self.assertIs(
            payload["engine_candidate_usable_for_future_migration"], True
        )

    def test_fallback_case_serializes_to_json_compatible_dict(self) -> None:
        result = assess(evidence=None)

        payload = scheduler_execution_engine_fallback_assessment_to_json_dict(
            result
        )

        json.dumps(payload)
        self.assertIs(payload["fallback_required"], True)
        self.assertEqual(payload["fallback_reason"], "engine_evidence_absent")
        self.assertEqual(payload["fallback_reasons"], ["engine_evidence_absent"])


class FilesystemSafetyTests(unittest.TestCase):
    def test_assessment_does_not_touch_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = make_evidence()
            evidence["request"]["workspace"] = {
                "repo_path": str(Path(tmp) / "missing-repo"),
                "artifact_dir": str(Path(tmp) / "missing-artifacts"),
            }

            assess(evidence=evidence)
            assess(evidence=None)

            self.assertEqual(list(Path(tmp).iterdir()), [])


class FallbackPurityTests(unittest.TestCase):
    """The module must stay a pure classification layer with no runtime calls."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(fallback_module.__file__).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_module_imports_no_runtime_modules(self) -> None:
        forbidden = (
            "approved_task_runner",
            "execution_engine_approved_task_adapter",
            "execution_engine_manual_runtime",
            "scheduler_execution_engine_opt_in",
            "github_issue_one_task_scheduler_tick",
            "github_issue_one_task_automation",
            "subprocess",
            "sqlite3",
            "requests",
            "urllib",
            "socket",
            "shutil",
            "os",
        )
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for name in imported:
            parts = name.split(".")
            for banned in forbidden:
                self.assertNotIn(banned, parts, msg=name)

    def test_module_does_not_call_engine_or_runtime_entrypoints(self) -> None:
        forbidden_calls = {
            "execute",
            "run_approved_task",
            "run",
            "Popen",
            "call",
            "check_call",
            "check_output",
            "system",
            "mkdir",
            "makedirs",
            "open",
            "write_text",
            "write_bytes",
            "unlink",
            "rmdir",
            "connect",
        }
        called: set[str] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        self.assertEqual(called & forbidden_calls, set())

    def test_module_identifiers_do_not_reference_cron(self) -> None:
        identifiers = {
            node.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
        }
        for identifier in identifiers:
            self.assertNotIn("cron", identifier.lower(), msg=identifier)

    def test_public_api_is_assessment_only(self) -> None:
        self.assertEqual(
            set(fallback_module.__all__),
            {
                "EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER",
                "SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION",
                "SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE",
                "SchedulerExecutionEngineFallbackAssessment",
                "SchedulerExecutionEngineFallbackAssessmentInput",
                "assess_scheduler_execution_engine_fallback",
                "scheduler_execution_engine_fallback_assessment_to_json_dict",
            },
        )


if __name__ == "__main__":
    unittest.main()
