"""Review evidence contract tests for workflow policy artifacts.

These tests verify that the existing review evidence helper classifies
canonical workflow policy artifacts with the canonical review evidence kind
"workflow_policy", using the shared constants from
agent_taskflow.workflow_policy_artifacts.

This is a tests-only patch. No runtime behavior is added or modified.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow.api.review import build_artifact_file_summaries, _file_kind
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_ARTIFACT_FILENAMES,
)


class WorkflowPolicyReviewEvidenceContractTests(unittest.TestCase):
    """Verify review evidence helper classifies workflow policy artifacts correctly."""

    # ------------------------------------------------------------------
    # _file_kind unit tests for workflow policy filenames
    # ------------------------------------------------------------------

    def test_file_kind_workflow_policy_summary_is_workflow_policy(self) -> None:
        kind = _file_kind(WORKFLOW_POLICY_SUMMARY_FILENAME)
        self.assertEqual(kind, WORKFLOW_POLICY_REVIEW_KIND)

    def test_file_kind_artifact_index_is_workflow_policy(self) -> None:
        kind = _file_kind(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME)
        self.assertEqual(kind, WORKFLOW_POLICY_REVIEW_KIND)

    # ------------------------------------------------------------------
    # Non-canonical filenames should NOT be classified as workflow_policy
    # ------------------------------------------------------------------

    def test_file_kind_workflow_policy_summary_txt_is_not_workflow_policy(self) -> None:
        kind = _file_kind("workflow_policy_summary.txt")
        self.assertNotEqual(kind, WORKFLOW_POLICY_REVIEW_KIND)

    def test_file_kind_artifact_index_json_variants_not_workflow_policy(self) -> None:
        # Slight name variants should not match.
        for name in (
            "artifact-index.json",
            "artifact_index.json.bak",
            "artifact_index.JSON",
        ):
            with self.subTest(name=name):
                kind = _file_kind(name)
                self.assertNotEqual(
                    kind,
                    WORKFLOW_POLICY_REVIEW_KIND,
                    f"{name!r} should not be classified as {WORKFLOW_POLICY_REVIEW_KIND!r}",
                )

    def test_file_kind_random_json_is_not_workflow_policy(self) -> None:
        for name in (
            "random.json",
            "summary.json",
            "index.json",
            "policy.json",
            "workflow.json",
        ):
            with self.subTest(name=name):
                kind = _file_kind(name)
                self.assertNotEqual(
                    kind,
                    WORKFLOW_POLICY_REVIEW_KIND,
                    f"{name!r} should not be classified as workflow_policy",
                )

    # ------------------------------------------------------------------
    # build_artifact_file_summaries tests with canonical filenames
    # ------------------------------------------------------------------

    def test_build_artifact_file_summaries_workflow_policy_summary_has_correct_kind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
                '{"artifact_type":"workflow_policy_summary"}', encoding="utf-8"
            )

            results = build_artifact_file_summaries(artifact_dir)
            self.assertEqual(len(results), 1)
            summary = results[0]
            self.assertEqual(summary["name"], WORKFLOW_POLICY_SUMMARY_FILENAME)
            self.assertEqual(summary["kind"], WORKFLOW_POLICY_REVIEW_KIND)
            self.assertIn("size_bytes", summary)

    def test_build_artifact_file_summaries_artifact_index_has_correct_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).write_text(
                '{"artifact_index_version":"0.1"}', encoding="utf-8"
            )

            results = build_artifact_file_summaries(artifact_dir)
            self.assertEqual(len(results), 1)
            summary = results[0]
            self.assertEqual(summary["name"], WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME)
            self.assertEqual(summary["kind"], WORKFLOW_POLICY_REVIEW_KIND)
            self.assertIn("size_bytes", summary)

    def test_both_canonical_workflow_policy_files_coexist_and_both_are_workflow_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
                '{"artifact_type":"workflow_policy_summary"}', encoding="utf-8"
            )
            (artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).write_text(
                '{"artifact_index_version":"0.1"}', encoding="utf-8"
            )

            results = build_artifact_file_summaries(artifact_dir)
            self.assertEqual(len(results), 2)

            by_name = {r["name"]: r for r in results}
            self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, by_name)
            self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, by_name)

            summary_artifact = by_name[WORKFLOW_POLICY_SUMMARY_FILENAME]
            self.assertEqual(summary_artifact["kind"], WORKFLOW_POLICY_REVIEW_KIND)
            self.assertIn("size_bytes", summary_artifact)

            index_artifact = by_name[WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME]
            self.assertEqual(index_artifact["kind"], WORKFLOW_POLICY_REVIEW_KIND)
            self.assertIn("size_bytes", index_artifact)

    # ------------------------------------------------------------------
    # Existing file kind behavior is preserved
    # ------------------------------------------------------------------

    def test_mission_contract_still_maps_to_mission_contract(self) -> None:
        kind = _file_kind("mission_contract.json")
        self.assertEqual(kind, "mission_contract")

    def test_validator_log_still_maps_to_validator_log(self) -> None:
        for name in ("pytest.log", "openspec-validate.log", "lint.log"):
            with self.subTest(name=name):
                kind = _file_kind(name)
                self.assertEqual(kind, "validator_log")

    def test_executor_log_still_maps_to_executor_log(self) -> None:
        for name in ("pi-executor.log", "opencode-executor.log", "pi-run.log"):
            with self.subTest(name=name):
                kind = _file_kind(name)
                self.assertEqual(kind, "executor_log")

    def test_unrelated_files_map_to_other(self) -> None:
        for name in (
            "handoff_summary.md",
            "run_summary.json",
            "decision.json",
            "README.txt",
        ):
            with self.subTest(name=name):
                kind = _file_kind(name)
                self.assertEqual(kind, "other")

    def test_build_artifact_file_summaries_existing_kinds_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "mission_contract.json").write_text(
                '{"schema_version":"1","task_key":"TEST"}', encoding="utf-8"
            )
            (artifact_dir / "pytest.log").write_text("passed", encoding="utf-8")
            (artifact_dir / "pi-executor.log").write_text("done", encoding="utf-8")

            results = build_artifact_file_summaries(artifact_dir)
            by_name = {r["name"]: r for r in results}

            self.assertEqual(
                by_name["mission_contract.json"]["kind"], "mission_contract"
            )
            self.assertEqual(by_name["pytest.log"]["kind"], "validator_log")
            self.assertEqual(by_name["pi-executor.log"]["kind"], "executor_log")

    # ------------------------------------------------------------------
    # Constants are used (not duplicated hardcoded values)
    # ------------------------------------------------------------------

    def test_constants_imported_from_workflow_policy_artifacts(self) -> None:
        # Verify the constants are imported from the shared module.
        self.assertEqual(WORKFLOW_POLICY_REVIEW_KIND, "workflow_policy")
        self.assertEqual(
            WORKFLOW_POLICY_SUMMARY_FILENAME, "workflow_policy_summary.json"
        )
        self.assertEqual(
            WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, "artifact_index.json"
        )
        self.assertEqual(
            WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, "workflow_policy_summary"
        )
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE, "artifact_index")

    def test_workflow_policy_artifact_filenames_frozenset_contains_both(self) -> None:
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, WORKFLOW_POLICY_ARTIFACT_FILENAMES)
        self.assertIn(
            WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, WORKFLOW_POLICY_ARTIFACT_FILENAMES
        )
        self.assertEqual(len(WORKFLOW_POLICY_ARTIFACT_FILENAMES), 2)

    # ------------------------------------------------------------------
    # Response shape: existing fields are present
    # ------------------------------------------------------------------

    def test_build_artifact_file_summaries_returns_name_kind_size_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
                '{"artifact_type":"workflow_policy_summary"}', encoding="utf-8"
            )

            results = build_artifact_file_summaries(artifact_dir)
            self.assertEqual(len(results), 1)
            entry = results[0]

            # Existing fields that must be present.
            self.assertIn("name", entry)
            self.assertIn("kind", entry)
            self.assertIn("size_bytes", entry)

            # Derived boolean flags.
            self.assertFalse(entry["is_validator_log"])
            self.assertFalse(entry["is_executor_log"])
            self.assertFalse(entry["is_mission_contract"])

    def test_workflow_policy_artifacts_have_no_incorrect_derived_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).write_text(
                '{"artifact_type":"workflow_policy_summary"}', encoding="utf-8"
            )
            (artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).write_text(
                '{"artifact_index_version":"0.1"}', encoding="utf-8"
            )

            results = build_artifact_file_summaries(artifact_dir)
            for entry in results:
                self.assertFalse(
                    entry["is_validator_log"],
                    f"{entry['name']} should not be validator_log",
                )
                self.assertFalse(
                    entry["is_executor_log"],
                    f"{entry['name']} should not be executor_log",
                )
                self.assertFalse(
                    entry["is_mission_contract"],
                    f"{entry['name']} should not be mission_contract",
                )


if __name__ == "__main__":
    unittest.main()