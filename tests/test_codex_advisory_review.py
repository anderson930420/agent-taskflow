"""Tests for the dry-run Codex advisory reviewer contract."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow import codex_advisory_review as module
from agent_taskflow.codex_advisory_review import (
    CodexAdvisoryReviewError,
    CodexAdvisoryReviewRequest,
    build_review_payload,
    build_review_prompt,
    detect_evidence,
    generate_codex_advisory_review,
    validate_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs" / "codex-advisory-review.md"


class CodexAdvisoryReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_dir = Path(self.tmp.name) / "artifacts" / "GH-1234"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self) -> CodexAdvisoryReviewRequest:
        return CodexAdvisoryReviewRequest(
            task_key="GH-1234",
            artifact_dir=self.artifact_dir,
            repo_path=Path(self.tmp.name) / "repo",
            worktree_path=Path(self.tmp.name) / "worktree",
        )

    def _generate(self) -> module.CodexAdvisoryReviewResult:
        return generate_codex_advisory_review(self._request())

    def test_dry_run_writes_exactly_expected_artifacts(self) -> None:
        result = self._generate()

        written = sorted(p.name for p in self.artifact_dir.iterdir())
        self.assertEqual(
            written,
            [
                "codex-advisory-review-prompt.md",
                "codex-advisory-review.json",
                "codex-advisory-review.md",
            ],
        )
        for path in result.artifact_paths():
            self.assertTrue(path.is_file())

    def test_json_invariant_fields(self) -> None:
        result = self._generate()
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], "codex_advisory_review.v1")
        self.assertEqual(payload["reviewer"], "codex-cli")
        self.assertEqual(payload["review_status"], "not_run")
        self.assertIs(payload["validation_authority"], False)
        self.assertIs(payload["human_review_required"], True)
        self.assertEqual(payload["risk_level"], "unknown")
        self.assertEqual(payload["task_key"], "GH-1234")

    def test_json_includes_required_contract_keys(self) -> None:
        payload = build_review_payload(self._request(), detect_evidence(self.artifact_dir))
        for key in (
            "schema_version",
            "reviewer",
            "task_key",
            "review_status",
            "validation_authority",
            "human_review_required",
            "summary",
            "design_findings",
            "correctness_findings",
            "test_coverage_findings",
            "architecture_boundary_findings",
            "risk_level",
            "recommended_human_focus",
            "suggested_followups",
            "missing_evidence",
            "artifacts",
        ):
            self.assertIn(key, payload, key)

    def test_validate_payload_rejects_validation_authority_true(self) -> None:
        payload = build_review_payload(self._request(), detect_evidence(self.artifact_dir))
        payload["validation_authority"] = True
        with self.assertRaises(CodexAdvisoryReviewError):
            validate_payload(payload)

    def test_validate_payload_rejects_human_review_required_false(self) -> None:
        payload = build_review_payload(self._request(), detect_evidence(self.artifact_dir))
        payload["human_review_required"] = False
        with self.assertRaises(CodexAdvisoryReviewError):
            validate_payload(payload)

    def test_validate_payload_rejects_invalid_review_statuses(self) -> None:
        base = build_review_payload(self._request(), detect_evidence(self.artifact_dir))
        for invalid in ("approved", "passed", "failed", "blocked", "merge_ready"):
            payload = dict(base)
            payload["review_status"] = invalid
            with self.assertRaises(CodexAdvisoryReviewError):
                validate_payload(payload)

    def test_validate_payload_rejects_invalid_risk_level(self) -> None:
        payload = build_review_payload(self._request(), detect_evidence(self.artifact_dir))
        payload["risk_level"] = "catastrophic"
        with self.assertRaises(CodexAdvisoryReviewError):
            validate_payload(payload)

    def test_validate_payload_accepts_dry_run_defaults(self) -> None:
        payload = build_review_payload(self._request(), detect_evidence(self.artifact_dir))
        validate_payload(payload)  # should not raise

    def test_prompt_includes_advisory_only_language(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        lowered = prompt.lower()
        self.assertIn("codex is an advisory reviewer only", lowered)
        self.assertIn("codex is not deterministic validation authority", lowered)

    def test_prompt_includes_deterministic_validator_boundary(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        lowered = prompt.lower()
        self.assertIn("pytest", lowered)
        self.assertIn("compileall", lowered)
        self.assertIn("policy", lowered)
        self.assertIn("changed-files", lowered)

    def test_prompt_includes_human_approval_required(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        self.assertIn("Human final approval is required", prompt)

    def test_prompt_includes_governance_prohibitions(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        lowered = prompt.lower()
        for needle in (
            "approve",
            "block",
            "merge",
            "push",
            "cleanup",
            "delete branch",
            "delete worktree",
            "lifecycle",
        ):
            self.assertIn(needle, lowered, needle)

    def test_prompt_references_review_dimensions(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        for dimension in (
            "Task fit",
            "Architecture fit",
            "Minimality",
            "Correctness risk",
            "Test adequacy",
            "Failure behavior",
            "Security / governance",
            "Human review focus",
        ):
            self.assertIn(dimension, prompt, dimension)

    def test_prompt_references_common_evidence_files(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        for evidence_file in (
            "task_execution_package.json",
            "implementation_prompt.md",
            "mission_contract.json",
            "executor logs",
            "pytest.log",
            "compileall.log",
            "policy-validate.log",
            "changed-files-audit.json",
        ):
            self.assertIn(evidence_file, prompt, evidence_file)

    def test_evidence_detection_is_executor_neutral(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / "task_execution_package.json").write_text("{}", encoding="utf-8")
        # Generic, executor-neutral log discovery (not pi/opencode/shell specific).
        (self.artifact_dir / "claude-code-executor.log").write_text("ok", encoding="utf-8")
        (self.artifact_dir / "pi-executor.log").write_text("ok", encoding="utf-8")

        evidence = detect_evidence(self.artifact_dir)
        self.assertIn("task_execution_package.json", evidence["present_evidence"])
        log_names = {item["name"] for item in evidence["executor_logs"]}
        self.assertIn("claude-code-executor.log", log_names)
        self.assertIn("pi-executor.log", log_names)

    def test_missing_evidence_reported_in_payload(self) -> None:
        result = self._generate()
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))
        self.assertIn("implementation_prompt.md", payload["missing_evidence"])

    def test_dry_run_does_not_invoke_subprocess(self) -> None:
        with mock.patch.object(subprocess, "run") as run_mock, mock.patch.object(
            subprocess, "Popen"
        ) as popen_mock, mock.patch.object(subprocess, "call") as call_mock:
            self._generate()
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        call_mock.assert_not_called()

    def test_module_source_has_no_subprocess_or_lifecycle_mutation(self) -> None:
        source = (REPO_ROOT / "agent_taskflow" / "codex_advisory_review.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "import subprocess",
            "subprocess.run",
            "Popen",
            "git push",
            "gh pr create",
            "record_approval_decision",
            "delete_worktree",
            "git worktree remove",
            "git branch -d",
        ):
            self.assertNotIn(forbidden, source, forbidden)

    def test_docs_mention_advisory_only_and_non_authoritative(self) -> None:
        self.assertTrue(DOCS.exists())
        text = DOCS.read_text(encoding="utf-8").lower()
        self.assertIn("advisory", text)
        self.assertIn("not", text)
        self.assertIn("deterministic validator", text)
        self.assertIn("human", text)
        self.assertIn("p5-f", text)
        self.assertIn("claude code executor", text)


if __name__ == "__main__":
    unittest.main()
