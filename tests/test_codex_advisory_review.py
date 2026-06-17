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

    def test_request_rejects_non_dry_run_mode(self) -> None:
        with self.assertRaises(ValueError):
            CodexAdvisoryReviewRequest(
                task_key="GH-1234",
                artifact_dir=self.artifact_dir,
                dry_run=False,
            )

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

    def test_module_source_has_no_lifecycle_mutation_or_shell(self) -> None:
        source = (REPO_ROOT / "agent_taskflow" / "codex_advisory_review.py").read_text(
            encoding="utf-8"
        )
        # The module invokes the Codex CLI in confirm-run mode, but it must never
        # use a shell or perform any lifecycle / governance mutation.
        for forbidden in (
            "shell=True",
            "Popen",
            "git push",
            "gh pr create",
            "record_approval_decision",
            "delete_worktree",
            "git worktree remove",
            "git branch -d",
        ):
            self.assertNotIn(forbidden, source, forbidden)
        # Confirm-run must invoke Codex with shell=False only.
        self.assertIn("shell=False", source)

    def test_module_does_not_import_scheduler_lifecycle_or_approval(self) -> None:
        source = (REPO_ROOT / "agent_taskflow" / "codex_advisory_review.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "import agent_taskflow.scheduler",
            "import agent_taskflow.dispatcher",
            "execution_engine",
            "ExecutionEngine",
            "approved_task_runner",
            "waiting_approval",
            "from agent_taskflow.scheduler",
            "from agent_taskflow.dispatcher",
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

    def test_docs_mention_confirm_run_advisory_and_excluded_scope(self) -> None:
        text = DOCS.read_text(encoding="utf-8").lower()
        self.assertIn("--confirm-run", text)
        self.assertIn("waiting_approval", text)
        self.assertIn("v0.2.2", text)

    def test_prompt_includes_json_output_instruction(self) -> None:
        prompt = build_review_prompt(self._request(), detect_evidence(self.artifact_dir))
        self.assertIn("Return a JSON object", prompt)
        lowered = prompt.lower()
        self.assertIn("do not claim validation authority", lowered)
        self.assertIn("do not claim approval authority", lowered)


class CodexAdvisoryConfirmRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifact_dir = self.root / "artifacts" / "GH-2222"
        self.repo = self.root / "repo"
        self.worktree = self.root / "worktree"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, **overrides) -> CodexAdvisoryReviewRequest:
        kwargs = dict(
            task_key="GH-2222",
            artifact_dir=self.artifact_dir,
            repo_path=self.repo,
            worktree_path=self.worktree,
            dry_run=False,
            confirm_run=True,
            codex_command="codex review",
            timeout_seconds=120,
        )
        kwargs.update(overrides)
        return CodexAdvisoryReviewRequest(**kwargs)

    def _completed(self, *, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(
            args=["codex"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def _run(self, *, request=None, **patch_kwargs):
        request = request or self._request()
        with mock.patch.object(
            module.subprocess, "run", **patch_kwargs
        ) as run_mock:
            result = generate_codex_advisory_review(request)
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))
        return result, payload, run_mock

    # --- request validation -------------------------------------------------

    def test_request_rejects_dry_run_and_confirm_run_together(self) -> None:
        with self.assertRaises(ValueError):
            CodexAdvisoryReviewRequest(
                task_key="GH-2222",
                artifact_dir=self.artifact_dir,
                dry_run=True,
                confirm_run=True,
            )

    def test_request_rejects_non_positive_timeout(self) -> None:
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                self._request(timeout_seconds=bad)

    def test_request_rejects_empty_codex_command(self) -> None:
        with self.assertRaises(ValueError):
            self._request(codex_command="   ")

    # --- invocation behavior ------------------------------------------------

    def test_confirm_run_invokes_subprocess_once_with_shell_false(self) -> None:
        fake = self._completed(stdout='{"review_status": "looks_good"}')
        result, payload, run_mock = self._run(return_value=fake)
        run_mock.assert_called_once()
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], ["codex", "review"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(kwargs["cwd"], str(self.worktree.resolve()))
        prompt_text = result.prompt_path.read_text(encoding="utf-8")
        self.assertEqual(kwargs["input"], prompt_text)
        self.assertIs(payload["codex_cli_invoked"], True)
        self.assertIs(payload["subprocess_invoked"], True)

    def test_confirm_run_cwd_falls_back_to_repo_then_none(self) -> None:
        fake = self._completed(stdout='{"review_status": "looks_good"}')
        _, _, run_mock = self._run(
            request=self._request(worktree_path=None), return_value=fake
        )
        self.assertEqual(run_mock.call_args.kwargs["cwd"], str(self.repo.resolve()))

        request = self._request(
            worktree_path=None,
            repo_path=None,
            artifact_dir=self.root / "artifacts" / "GH-2223",
        )
        _, _, run_mock = self._run(request=request, return_value=fake)
        self.assertIsNone(run_mock.call_args.kwargs["cwd"])

    def test_confirm_run_writes_stdout_stderr_artifacts(self) -> None:
        fake = self._completed(stdout="raw-out", stderr="raw-err")
        result, _, _ = self._run(return_value=fake)
        self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "raw-out")
        self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "raw-err")
        self.assertTrue(
            (self.artifact_dir / "codex-advisory-review-stdout.txt").is_file()
        )
        self.assertTrue(
            (self.artifact_dir / "codex-advisory-review-stderr.txt").is_file()
        )

    def test_dry_run_does_not_write_stdout_stderr_artifacts(self) -> None:
        request = CodexAdvisoryReviewRequest(
            task_key="GH-2222", artifact_dir=self.artifact_dir
        )
        generate_codex_advisory_review(request)
        self.assertFalse(
            (self.artifact_dir / "codex-advisory-review-stdout.txt").exists()
        )
        self.assertFalse(
            (self.artifact_dir / "codex-advisory-review-stderr.txt").exists()
        )

    # --- successful Codex JSON ----------------------------------------------

    def test_confirm_run_parses_raw_json_looks_good(self) -> None:
        payload_in = {
            "review_status": "looks_good",
            "summary": "all good",
            "risk_level": "low",
            "design_findings": ["d1"],
        }
        fake = self._completed(stdout=json.dumps(payload_in))
        _, payload, _ = self._run(return_value=fake)
        self.assertEqual(payload["review_status"], "looks_good")
        self.assertEqual(payload["summary"], "all good")
        self.assertEqual(payload["risk_level"], "low")
        self.assertEqual(payload["design_findings"], ["d1"])

    def test_confirm_run_parses_fenced_json_needs_attention(self) -> None:
        body = (
            "Here is my advisory review:\n\n```json\n"
            + json.dumps(
                {
                    "review_status": "needs_attention",
                    "correctness_findings": ["c1", "c2"],
                }
            )
            + "\n```\n\nThanks."
        )
        fake = self._completed(stdout=body)
        _, payload, _ = self._run(return_value=fake)
        self.assertEqual(payload["review_status"], "needs_attention")
        self.assertEqual(payload["correctness_findings"], ["c1", "c2"])

    def test_confirm_run_canonical_fields_override_codex(self) -> None:
        payload_in = {
            "review_status": "looks_good",
            "schema_version": "hacked",
            "reviewer": "evil",
            "task_key": "GH-EVIL",
            "artifacts": {"x": "y"},
            "generated_at": "1999",
            "governance": {"advisory_only": False},
        }
        fake = self._completed(stdout=json.dumps(payload_in))
        _, payload, _ = self._run(return_value=fake)
        self.assertEqual(payload["schema_version"], "codex_advisory_review.v1")
        self.assertEqual(payload["reviewer"], "codex-cli")
        self.assertEqual(payload["task_key"], "GH-2222")
        self.assertNotEqual(payload["artifacts"], {"x": "y"})
        self.assertIs(payload["governance"]["advisory_only"], True)

    def test_confirm_run_rejects_codex_validation_authority_true(self) -> None:
        fake = self._completed(
            stdout=json.dumps(
                {"review_status": "looks_good", "validation_authority": True}
            )
        )
        _, payload, _ = self._run(return_value=fake)
        self.assertIs(payload["validation_authority"], False)
        self.assertEqual(payload["review_status"], "tool_error")
        self.assertEqual(
            payload["tool_error"]["category"], "codex_output_invariant_violation"
        )

    def test_confirm_run_rejects_codex_human_review_required_false(self) -> None:
        fake = self._completed(
            stdout=json.dumps(
                {"review_status": "looks_good", "human_review_required": False}
            )
        )
        _, payload, _ = self._run(return_value=fake)
        self.assertIs(payload["human_review_required"], True)
        self.assertEqual(payload["review_status"], "tool_error")

    # --- tool error cases ---------------------------------------------------

    def _assert_tool_error(self, payload, category=None) -> None:
        self.assertEqual(payload["review_status"], "tool_error")
        self.assertEqual(payload["risk_level"], "unknown")
        self.assertIs(payload["validation_authority"], False)
        self.assertIs(payload["human_review_required"], True)
        if category is not None:
            self.assertEqual(payload["tool_error"]["category"], category)

    def test_confirm_run_command_not_found_tool_error(self) -> None:
        _, payload, _ = self._run(side_effect=FileNotFoundError("codex"))
        self._assert_tool_error(payload, "codex_cli_not_found")

    def test_confirm_run_timeout_tool_error(self) -> None:
        exc = subprocess.TimeoutExpired(
            cmd=["codex"], timeout=120, output="partial", stderr="err"
        )
        _, payload, _ = self._run(side_effect=exc)
        self._assert_tool_error(payload, "codex_cli_timeout")
        self.assertIs(payload["codex_invocation"]["timed_out"], True)

    def test_confirm_run_nonzero_exit_tool_error(self) -> None:
        fake = self._completed(stdout='{"review_status": "looks_good"}', returncode=2)
        _, payload, _ = self._run(return_value=fake)
        self._assert_tool_error(payload, "codex_cli_nonzero_exit")
        self.assertEqual(payload["codex_invocation"]["exit_code"], 2)

    def test_confirm_run_unparseable_stdout_tool_error(self) -> None:
        fake = self._completed(stdout="not json at all")
        _, payload, _ = self._run(return_value=fake)
        self._assert_tool_error(payload, "codex_output_parse_error")
        self.assertIsNotNone(payload["codex_invocation"]["parse_error"])

    def test_confirm_run_invalid_review_status_tool_error(self) -> None:
        for invalid in ("approved", "passed", "failed", "blocked", "merge_ready"):
            with self.subTest(invalid=invalid):
                fake = self._completed(stdout=json.dumps({"review_status": invalid}))
                _, payload, _ = self._run(return_value=fake)
                self._assert_tool_error(payload, "codex_output_invariant_violation")

    def test_confirm_run_invalid_risk_level_tool_error(self) -> None:
        fake = self._completed(
            stdout=json.dumps(
                {"review_status": "looks_good", "risk_level": "catastrophic"}
            )
        )
        _, payload, _ = self._run(return_value=fake)
        self._assert_tool_error(payload, "codex_output_invariant_violation")

    # --- authority boundary -------------------------------------------------

    def test_confirm_run_high_risk_is_not_a_validation_failure(self) -> None:
        fake = self._completed(
            stdout=json.dumps({"review_status": "high_risk", "risk_level": "high"})
        )
        _, payload, _ = self._run(return_value=fake)
        self.assertEqual(payload["review_status"], "high_risk")
        self.assertIs(payload["validation_authority"], False)
        self.assertIs(payload["human_review_required"], True)

    def test_confirm_run_markdown_shows_invocation_and_advisory_note(self) -> None:
        fake = self._completed(stdout="oops")
        result, _, _ = self._run(return_value=fake)
        md = result.markdown_path.read_text(encoding="utf-8")
        self.assertIn("Confirm Run", md)
        self.assertIn("Codex CLI invoked: True", md)
        self.assertIn("advisory signal only", md.lower())
        self.assertIn("Tool Error", md)


if __name__ == "__main__":
    unittest.main()
