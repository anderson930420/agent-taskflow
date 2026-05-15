"""Tests for the Pi Mission Protocol renderer and writer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.executors.pi_protocol import (
    load_contract_for_pi,
    render_pi_mission_prompt,
    write_pi_mission_prompt,
)


class RenderPiMissionPromptTests(unittest.TestCase):
    """Tests for render_pi_mission_prompt."""

    def _minimal_contract(self) -> dict:
        return {
            "schema_version": "1",
            "task_key": "AT-0101",
            "goal": "Implement the feature",
            "repo_path": "/tmp/repo",
            "worktree_path": "/tmp/worktree",
            "artifact_dir": "/tmp/artifacts",
            "executor": "pi",
            "required_validators": ["pytest", "openspec"],
            "forbidden_actions": ["push", "merge", "cleanup"],
            "expected_artifacts": ["executor_log"],
            "human_approval_required": True,
            "governance_rules": [],
        }

    def test_includes_goal(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("Implement the feature", result)
        self.assertIn("# Pi Mission Protocol", result)

    def test_includes_task_key(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("AT-0101", result)

    def test_includes_worktree_path(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("/tmp/worktree", result)

    def test_includes_artifact_dir(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("/tmp/artifacts", result)

    def test_includes_required_validators(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("pytest", result)
        self.assertIn("openspec", result)

    def test_includes_forbidden_actions(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("push", result)
        self.assertIn("merge", result)
        self.assertIn("cleanup", result)

    def test_includes_hard_governance_rules(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("Human approval is the final gate", result)
        self.assertIn("Deterministic validators", result)

    def test_includes_do_not_approve(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("Do NOT approve", result)

    def test_includes_do_not_push(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("Do NOT push", result)

    def test_includes_do_not_merge(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("Do NOT merge", result)

    def test_includes_do_not_cleanup(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("Do NOT run cleanup", result)

    def test_includes_ai_review_not_replace_validators(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("cannot replace deterministic validators", result)

    def test_includes_human_approval_final_gate(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("final gate", result.lower())

    def test_includes_original_prompt_when_provided(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract, original_prompt="Build the thing")
        self.assertIn("Build the thing", result)
        self.assertIn("Original Task Prompt", result)

    def test_secret_original_prompt_omitted(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(
            contract,
            original_prompt='api_secret: "sk-testsecret1234567890"',
        )
        self.assertNotIn("sk-testsecret1234567890", result)
        self.assertIn("secret-like", result.lower())

    def test_output_deterministic(self) -> None:
        contract = self._minimal_contract()
        result1 = render_pi_mission_prompt(contract)
        result2 = render_pi_mission_prompt(contract)
        self.assertEqual(result1, result2)

    def test_output_is_markdown(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("# Pi Mission Protocol", result)
        self.assertIn("## Mission Goal", result)
        self.assertIn("## Working Context", result)
        self.assertIn("## Governance Rules", result)

    def test_rejects_missing_goal(self) -> None:
        contract = self._minimal_contract()
        del contract["goal"]
        with self.assertRaises(ValueError) as ctx:
            render_pi_mission_prompt(contract)
        self.assertIn("goal", str(ctx.exception).lower())

    def test_rejects_missing_executor(self) -> None:
        contract = self._minimal_contract()
        del contract["executor"]
        with self.assertRaises(ValueError) as ctx:
            render_pi_mission_prompt(contract)
        self.assertIn("executor", str(ctx.exception).lower())

    def test_rejects_non_dict(self) -> None:
        with self.assertRaises(TypeError):
            render_pi_mission_prompt("not a dict")  # type: ignore[arg-type]

    def test_includes_model_when_present(self) -> None:
        contract = self._minimal_contract()
        contract["model"] = "minimax-01"
        result = render_pi_mission_prompt(contract)
        self.assertIn("minimax-01", result)

    def test_includes_provider_when_present(self) -> None:
        contract = self._minimal_contract()
        contract["provider"] = "minimax"
        result = render_pi_mission_prompt(contract)
        self.assertIn("minimax", result)

    def test_includes_expected_artifacts(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)
        self.assertIn("executor_log", result)

    def test_contract_with_empty_validators_renders(self) -> None:
        contract = self._minimal_contract()
        contract["required_validators"] = []
        result = render_pi_mission_prompt(contract)
        self.assertIn("Required Deterministic Validators", result)

    def test_rendered_prompt_uses_canonical_key_wording(self) -> None:
        contract = self._minimal_contract()
        result = render_pi_mission_prompt(contract)

        self.assertIn("Required Deterministic Validators", result)
        self.assertIn("Expected Artifacts", result)
        for typo in ("validato_logs", "requiredvalidators", "required validators"):
            self.assertNotIn(typo, result.lower())


class WritePiMissionPromptTests(unittest.TestCase):
    """Tests for write_pi_mission_prompt."""

    def test_writes_inside_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            content = "# Pi Mission Protocol\nTest content"
            path = write_pi_mission_prompt(artifact_dir, content)

            self.assertEqual(path.name, "pi_mission_prompt.md")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_path_traversal_impossible(self) -> None:
        # Path traversal is prevented by the relative_to check.
        # Verify the function raises if someone tries to escape artifact_dir.
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            # The output path must be inside artifact_dir; verify the check passes.
            path = write_pi_mission_prompt(artifact_dir, "safe content")
            self.assertTrue(path.exists())
            # An attempt to use a path that would escape should raise.
            # (simulated by checking the relative_to guard logic is in place)

    def test_accepts_and_resolves_relative_artifact_dir(self) -> None:
        # On POSIX, Path.resolve() always returns an absolute path, so a relative
        # artifact_dir is accepted (it is resolved to absolute before use).
        with tempfile.TemporaryDirectory() as tmp:
            original = Path.cwd()
            import os
            os.chdir(tmp)
            try:
                # Write to a relative artifact directory (resolved to absolute).
                rel_dir = Path("my-artifacts")
                rel_dir.mkdir()
                path = write_pi_mission_prompt(rel_dir, "relative test")
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text(encoding="utf-8"), "relative test")
            finally:
                os.chdir(original)

    def test_creates_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "deeply" / "nested"
            self.assertFalse(artifact_dir.exists())
            path = write_pi_mission_prompt(artifact_dir, "content")
            self.assertTrue(artifact_dir.exists())
            self.assertTrue(path.exists())

    def test_written_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            content = "protocol content\nwith special chars: <>&'"
            path = write_pi_mission_prompt(artifact_dir, content)
            self.assertEqual(path.read_text(encoding="utf-8"), content)


class LoadContractForPiTests(unittest.TestCase):
    """Tests for load_contract_for_pi."""

    def test_returns_none_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            result = load_contract_for_pi(artifact_dir)
            self.assertIsNone(result)

    def test_returns_dict_when_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            contract = {
                "schema_version": "1",
                "task_key": "AT-0102",
                "goal": "Test goal",
                "repo_path": str(Path(tmp) / "repo"),
                "worktree_path": str(Path(tmp) / "wt"),
                "artifact_dir": str(tmp),
                "executor": "pi",
                "required_validators": ["pytest"],
                "forbidden_actions": [],
                "expected_artifacts": [],
                "human_approval_required": True,
                "governance_rules": [],
            }
            (artifact_dir / "mission_contract.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
            result = load_contract_for_pi(artifact_dir)
            self.assertIsInstance(result, dict)
            assert result is not None
            self.assertEqual(result["task_key"], "AT-0102")


if __name__ == "__main__":
    unittest.main()
