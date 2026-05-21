"""Tests for the PolicyCheckValidator."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.validators.policy import (
    PolicyCheckValidator,
    _SECRET_PATTERNS,
    _SUSPICIOUS_ACTION_PATTERNS,
    _find_secret_assignments,
    _find_suspicious_actions,
    _scan_artifact_file,
)
from agent_taskflow.validators.base import ValidatorContext


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


def make_contract(**overrides) -> dict:
    """Return a valid contract dict merged with defaults.

    By default uses placeholder paths. Callers that need real paths should
    pass overrides for repo_path, worktree_path, and artifact_dir so they
    match the actual filesystem used in the test.
    """
    contract = {
        "schema_version": "1",
        "task_key": "AT-POL01",
        "goal": "Implement the feature.",
        "repo_path": "/tmp/repo",
        "worktree_path": "/tmp/repo/.worktrees/AT-POL01",
        "artifact_dir": "/tmp/artifacts",
        "executor": "manual",
        "required_validators": ["pytest", "openspec"],
        "forbidden_actions": [
            "approve",
            "push",
            "merge",
            "cleanup",
            "delete_worktree",
            "delete_branch",
            "self_approve",
            "force_push",
        ],
        "expected_artifacts": ["executor_log", "git_status"],
        "human_approval_required": True,
        "governance_rules": [
            "agent-taskflow is the governance/control plane.",
            "Human approval is the final gate.",
        ],
    }
    for k, v in overrides.items():
        if v is None:
            contract.pop(k, None)
        else:
            contract[k] = v
    return contract


def write_contract(artifact_dir: Path, contract: dict) -> Path:
    """Write contract to artifact_dir/mission_contract.json.

    Always patches contract["artifact_dir"] to str(artifact_dir). This ensures
    the policy validator's directory-existence check always sees a valid path
    for tests that don't specifically test artifact_dir failures.

    Tests that need to verify artifact_dir handling must write the contract
    file directly without using this helper.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    contract = dict(contract)  # don't mutate the caller's dict
    contract["artifact_dir"] = str(artifact_dir)
    path = artifact_dir / "mission_contract.json"
    path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    return path


def make_context(
    tmp: Path,
    *,
    task_key: str = "AT-POL01",
    scan_artifacts: bool = True,
) -> ValidatorContext:
    """Return a ValidatorContext backed by a temporary directory."""
    worktree = tmp / "worktree"
    artifact_dir = tmp / "artifacts"
    worktree.mkdir()
    artifact_dir.mkdir()
    return ValidatorContext(
        task_key=task_key,
        project="agent-taskflow",
        worktree_path=worktree,
        artifact_dir=artifact_dir,
        timeout_seconds=30,
    )


# ----------------------------------------------------------------------
# Constructor tests
# ----------------------------------------------------------------------


class PolicyConstructorTests(unittest.TestCase):
    def test_default_constructor(self) -> None:
        v = PolicyCheckValidator()
        self.assertEqual(v.name, "policy")
        self.assertTrue(v.scan_artifacts)

    def test_scan_artifacts_false(self) -> None:
        v = PolicyCheckValidator(scan_artifacts=False)
        self.assertFalse(v.scan_artifacts)

    def test_rejects_non_bool_scan_artifacts(self) -> None:
        with self.assertRaises(TypeError):
            PolicyCheckValidator(scan_artifacts="true")  # type: ignore[arg-type]

    def test_max_scan_size_default(self) -> None:
        v = PolicyCheckValidator()
        self.assertEqual(v.max_scan_size, 1024 * 1024)


# ----------------------------------------------------------------------
# Regex pattern tests
# ----------------------------------------------------------------------


class SecretPatternTests(unittest.TestCase):
    def test_matches_api_key_assignment(self) -> None:
        findings = _find_secret_assignments('export OPENAI_API_KEY=sk-test1234567890ab')
        self.assertTrue(len(findings) > 0)

    def test_matches_token_assignment(self) -> None:
        # "TOKEN": "..." JSON-style assignment
        findings = _find_secret_assignments('"TOKEN": "ghp_secret_token_here"')
        self.assertTrue(len(findings) > 0)

    def test_matches_json_secret(self) -> None:
        findings = _find_secret_assignments('{"password": "hunter2"}')
        self.assertTrue(len(findings) > 0)

    def test_matches_github_token_pattern(self) -> None:
        # Compound key ending in TOKEN= triggers the key assignment pattern
        findings = _find_secret_assignments('GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx')
        self.assertTrue(len(findings) > 0)

    def test_ignores_documentation_mention(self) -> None:
        # A doc that says "do not store API_KEY in the repo" should NOT match
        text = "Do not store API_KEY in the repository. Use environment variables."
        findings = _find_secret_assignments(text)
        self.assertEqual(len(findings), 0)

    def test_ignores_keyword_in_sentence(self) -> None:
        text = "When setting TOKEN, make sure it is valid."
        findings = _find_secret_assignments(text)
        self.assertEqual(len(findings), 0)

    # ------------------------------------------------------------------
    # Phase 60: camelCase false-positive tests
    # These identifiers contain "Key" as a camelCase segment but are NOT
    # secret assignments. The policy validator must NOT flag them.
    # ------------------------------------------------------------------
    def test_ignores_normalized_task_key_camel_case(self) -> None:
        """normalizedTaskKey = taskKey.trim() is not a secret assignment."""
        findings = _find_secret_assignments(
            "const normalizedTaskKey = taskKey.trim();"
        )
        self.assertEqual(len(findings), 0)

    def test_ignores_task_key_identifier(self) -> None:
        """let taskKey = input.trim() is not a secret assignment."""
        findings = _find_secret_assignments("let taskKey = input.trim();")
        self.assertEqual(len(findings), 0)

    def test_ignores_artifact_key_identifier(self) -> None:
        """const artifactKey = taskKey + ":" is not a secret assignment."""
        findings = _find_secret_assignments(
            'const artifactKey = taskKey + ":";'
        )
        self.assertEqual(len(findings), 0)

    def test_ignores_key_name_identifier(self) -> None:
        """const keyName = \"task_key\"; is not a secret assignment."""
        findings = _find_secret_assignments('const keyName = "task_key";')
        self.assertEqual(len(findings), 0)

    def test_ignores_public_key_label(self) -> None:
        """const publicKeyLabel = \"Public key\"; is not a secret assignment."""
        findings = _find_secret_assignments(
            'const publicKeyLabel = "Public key";'
        )
        self.assertEqual(len(findings), 0)

    def test_ignores_token_value_identifier(self) -> None:
        """let tokenValue = getToken(); is not a secret assignment."""
        findings = _find_secret_assignments("let tokenValue = getToken();")
        self.assertEqual(len(findings), 0)

    def test_ignores_api_key_label(self) -> None:
        """API_KEY_LABEL is a label, not a secret."""
        findings = _find_secret_assignments(
            'const API_KEY_LABEL = "test";'
        )
        self.assertEqual(len(findings), 0)

    def test_ignores_json_task_key_field(self) -> None:
        """JSON {\"task_key\": \"AT-123\"} is not a secret."""
        findings = _find_secret_assignments('{"task_key": "AT-123"}')
        self.assertEqual(len(findings), 0)

    def test_ignores_lowercase_token_identifier(self) -> None:
        """const token = \"test\" is not a secret assignment."""
        findings = _find_secret_assignments('const token = "test"')
        self.assertEqual(len(findings), 0)

    # ------------------------------------------------------------------
    # Phase 60: true-positive tests
    # These MUST still be flagged as secret assignments.
    # ------------------------------------------------------------------
    def test_matches_bare_password_assignment(self) -> None:
        """PASSWORD=\"hunter2\" must be flagged."""
        findings = _find_secret_assignments('PASSWORD="hunter2"')
        self.assertTrue(len(findings) > 0)

    def test_matches_bare_secret_assignment(self) -> None:
        """SECRET=\"super-secret\" must be flagged."""
        findings = _find_secret_assignments('SECRET="super-secret"')
        self.assertTrue(len(findings) > 0)

    def test_matches_api_key_number_suffix(self) -> None:
        """API_KEY_123=\"value\" must be flagged."""
        findings = _find_secret_assignments('API_KEY_123="value"')
        self.assertTrue(len(findings) > 0)

    def test_ignores_bare_token_as_compound_prefix(self) -> None:
        """TOKEN_VALUE=\"test\" is a generic identifier, not a secret.

        Only TOKEN as a standalone suffix (SECRET_TOKEN=) or bare TOKEN= at word
        boundary would be a concern. TOKEN_VALUE where TOKEN is a prefix is not a
        high-confidence secret indicator.
        """
        findings = _find_secret_assignments('TOKEN_VALUE="test"')
        self.assertEqual(len(findings), 0)

    def test_matches_standalone_token_as_label(self) -> None:
        """OPENAI_API_KEY: (label-style) must be flagged."""
        findings = _find_secret_assignments("OPENAI_API_KEY:")
        self.assertTrue(len(findings) > 0)

    def test_matches_client_secret_snake_case(self) -> None:
        """client_secret = \"abc123\" must be flagged."""
        findings = _find_secret_assignments('client_secret = "abc123"')
        self.assertTrue(len(findings) > 0)

    def test_matches_json_access_token(self) -> None:
        """{\"access_token\": \"token-value\"} must be flagged."""
        findings = _find_secret_assignments('{"access_token": "token-value"}')
        self.assertTrue(len(findings) > 0)

    def test_matches_my_secret_compound(self) -> None:
        """MY_SECRET=\"value\" must be flagged."""
        findings = _find_secret_assignments('MY_SECRET="value"')
        self.assertTrue(len(findings) > 0)

    def test_matches_my_password_compound(self) -> None:
        """MY_PASSWORD=\"hunter2\" must be flagged."""
        findings = _find_secret_assignments('MY_PASSWORD="hunter2"')
        self.assertTrue(len(findings) > 0)

    def test_matches_bare_credential(self) -> None:
        """CREDENTIAL=abc123 must be flagged."""
        findings = _find_secret_assignments("CREDENTIAL=abc123")
        self.assertTrue(len(findings) > 0)


class SuspiciousActionPatternTests(unittest.TestCase):
    def test_matches_git_push(self) -> None:
        findings = _find_suspicious_actions("Executing: git push origin main")
        self.assertTrue(len(findings) > 0)

    def test_matches_git_merge(self) -> None:
        findings = _find_suspicious_actions("git merge feature-123")
        self.assertTrue(len(findings) > 0)

    def test_matches_approve_task(self) -> None:
        findings = _find_suspicious_actions("approve task AT-001 completed")
        self.assertTrue(len(findings) > 0)

    def test_matches_cleanup_completed(self) -> None:
        findings = _find_suspicious_actions("cleanup completed for worktree AT-001")
        self.assertTrue(len(findings) > 0)

    def test_matches_delete_worktree(self) -> None:
        findings = _find_suspicious_actions("delete worktree .worktrees/AT-001")
        self.assertTrue(len(findings) > 0)

    def test_matches_delete_branch(self) -> None:
        findings = _find_suspicious_actions("delete branch feature-old")
        self.assertTrue(len(findings) > 0)

    def test_matches_rm_rf_worktrees(self) -> None:
        findings = _find_suspicious_actions("rm -rf .worktrees")
        self.assertTrue(len(findings) > 0)

    def test_matches_force_push(self) -> None:
        findings = _find_suspicious_actions("git push --force origin main")
        self.assertTrue(len(findings) > 0)

    def test_ignores_docs_about_push(self) -> None:
        # "do not push directly to main" is not a forbidden action
        text = "Do not push directly to main. Use PRs instead."
        findings = _find_suspicious_actions(text)
        self.assertEqual(len(findings), 0)

    def test_ignores_approve_in_a_sentence(self) -> None:
        text = "Reviewers may approve the PR after checking tests pass."
        findings = _find_suspicious_actions(text)
        self.assertEqual(len(findings), 0)


# ----------------------------------------------------------------------
# File scan tests
# ----------------------------------------------------------------------


class ScanArtifactFileTests(unittest.TestCase):
    def test_skips_binary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            secrets, actions = _scan_artifact_file(path)
            self.assertEqual(secrets, [])
            self.assertEqual(actions, [])

    def test_skips_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.txt"
            # Create a file larger than MAX_SCAN_SIZE (1 MB)
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            secrets, actions = _scan_artifact_file(path)
            self.assertEqual(secrets, [])
            self.assertEqual(actions, [])

    def test_finds_secret_in_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"api_key": "sk-test1234567890"}', encoding="utf-8")
            secrets, actions = _scan_artifact_file(path)
            self.assertTrue(len(secrets) > 0)
            self.assertEqual(actions, [])

    def test_finds_suspicious_action_in_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.txt"
            path.write_text("git push origin main\n", encoding="utf-8")
            secrets, actions = _scan_artifact_file(path)
            self.assertEqual(secrets, [])
            self.assertTrue(len(actions) > 0)


# ----------------------------------------------------------------------
# Validator run tests
# ----------------------------------------------------------------------


class PolicyValidatorRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_contract_passes(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validator, "policy")

    def test_missing_mission_contract_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()
        # No contract written

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator()
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("mission_contract.json not found", result.summary or "")

    def test_invalid_json_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        (artifact_dir / "mission_contract.json").write_text("not valid json {", encoding="utf-8")

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator()
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("not valid JSON", result.summary or "")

    def test_missing_required_field_goal_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract(goal=None))

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("goal", result.summary or "")

    def test_unsupported_schema_version_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract(schema_version="99"))

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("Unsupported schema_version", result.summary or "")

    def test_human_approval_required_false_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract(human_approval_required=False))

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("human_approval_required", result.summary or "")

    def test_missing_forbidden_action_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        # Remove "push" from forbidden_actions
        contract = make_contract()
        contract["forbidden_actions"] = ["approve", "merge"]
        write_contract(artifact_dir, contract)

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("forbidden_actions", result.summary or "")
        self.assertIn("push", result.summary or "")

    def test_empty_required_validators_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract(required_validators=[]))

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("required_validators", result.summary or "")

    def test_missing_artifact_dir_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        # Write contract WITHOUT artifact_dir key by bypassing write_contract's
        # auto-patch. This tests the "missing from contract" path.
        contract = make_contract()
        contract.pop("artifact_dir", None)  # remove it so contract has no key
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(contract, indent=2), encoding="utf-8"
        )

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")

    def test_artifact_dir_not_directory_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()
        artifact_file = self.root / "artifact_dir_as_file"
        artifact_file.write_text("x", encoding="utf-8")

        # Bypass write_contract's auto-patch so the contract records the
        # file path (not the directory). write_contract always patches
        # artifact_dir to str(artifact_dir), so we write manually here.
        artifact_dir.mkdir(parents=True, exist_ok=True)
        contract = make_contract(artifact_dir=str(artifact_file))
        contract["artifact_dir"] = str(artifact_file)  # force the bad path
        (artifact_dir / "mission_contract.json").write_text(
            json.dumps(contract, indent=2), encoding="utf-8"
        )

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("does not exist as a directory", result.summary or "")

    def test_suspicious_git_push_artifact_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        # Use a worker-produced artifact name (not pi-executor.log, which is
        # skipped because it contains embedded governance text in command args).
        # A real git-push violation would appear in a git status artifact or
        # worker log, not in the system-generated executor metadata log.
        log_file = artifact_dir / "git_status.txt"
        log_file.write_text(
            "Executing: git push origin feature-branch\n",
            encoding="utf-8",
        )

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("git push", result.summary or "")

    def test_suspicious_merge_artifact_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        log_file = artifact_dir / "events.jsonl"
        log_file.write_text(
            "gh pr merge --admin --repo myorg/myrepo\n",
            encoding="utf-8",
        )

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("pr merge", result.summary or "")

    def test_suspicious_cleanup_artifact_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        log_file = artifact_dir / "actions.log"
        log_file.write_text("cleanup completed for AT-001 worktree\n", encoding="utf-8")

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("cleanup", result.summary or "")

    def test_suspicious_delete_worktree_artifact_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        log_file = artifact_dir / "actions.log"
        log_file.write_text("rm -rf .worktrees/AT-001\n", encoding="utf-8")

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("worktree", result.summary or "")

    def test_secret_assignment_in_artifact_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        log_file = artifact_dir / "config.json"
        log_file.write_text(
            '{"OPENAI_API_KEY": "sk-test1234567890ab"}',
            encoding="utf-8",
        )

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("secret", result.summary or "")

    def test_documentation_mention_of_api_key_does_not_fail(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        # A doc that says "do not store API_KEY in the repo" should NOT fail
        doc_file = artifact_dir / "README.md"
        doc_file.write_text(
            "IMPORTANT: Do not store API_KEY in the repository. Use environment variables.",
            encoding="utf-8",
        )

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        # The contract is valid, and the README does not contain a secret assignment
        self.assertEqual(result.status, "passed")

    def test_binary_file_skipped_safely(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        # Create a PNG file (binary)
        binary_file = artifact_dir / "screenshot.png"
        binary_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        # Should pass — binary files are skipped
        self.assertEqual(result.status, "passed")

    def test_oversized_file_skipped(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        # Create a file larger than MAX_SCAN_SIZE
        large_file = artifact_dir / "large.log"
        large_file.write_bytes(b"x" * (1024 * 1024 + 100))

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=True)
        result = validator.run(context)

        # Should pass — oversized files are skipped
        self.assertEqual(result.status, "passed")

    def test_policy_validator_writes_log(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertIsNotNone(result.log_path)
        assert result.log_path is not None
        self.assertTrue(result.log_path.exists())
        log_content = result.log_path.read_text(encoding="utf-8")
        self.assertIn("Validator: policy", log_content)
        self.assertIn("AT-POL01", log_content)

    def test_policy_validator_returns_useful_failure_summary(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        # Write a contract with human_approval_required = false
        write_contract(artifact_dir, make_contract(human_approval_required=False))

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.summary)
        assert result.summary is not None
        # Summary should indicate what failed
        self.assertTrue(len(result.summary) > 10)

    def test_contract_as_object_not_array_fails(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        (artifact_dir / "mission_contract.json").write_text("[1, 2, 3]", encoding="utf-8")

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator()
        result = validator.run(context)

        self.assertEqual(result.status, "failed")
        self.assertIn("JSON object", result.summary or "")

    def test_scan_disabled_skips_artifact_scanning(self) -> None:
        worktree = self.root / "worktree"
        artifact_dir = self.root / "artifacts"
        worktree.mkdir()
        artifact_dir.mkdir()

        write_contract(artifact_dir, make_contract())

        # Create a suspicious artifact, but scanning is disabled.
        # Use a worker artifact name (not pi-executor.log) since the executor
        # log is skipped by the policy validator's scan filter.
        log_file = artifact_dir / "git_status.txt"
        log_file.write_text("git push origin main\n", encoding="utf-8")

        context = ValidatorContext(
            task_key="AT-POL01",
            project="agent-taskflow",
            worktree_path=worktree,
            artifact_dir=artifact_dir,
        )
        validator = PolicyCheckValidator(scan_artifacts=False)
        result = validator.run(context)

        # Should pass because artifact scanning is disabled
        self.assertEqual(result.status, "passed")


# ----------------------------------------------------------------------
# Phase 6E+3.3 — role-based scanning regression tests
#
# These verify the false-positive classes seen during Phase 6E+3 real
# OpenCode self-dogfood:
# 1. implementation_prompt.md governance prohibitions must not fail policy.
# 2. opencode-events.jsonl model reasoning / read-tool output must not fail.
# 3. Actual changed files / executor-written scripts with dangerous
#    behavior MUST still fail.
# ----------------------------------------------------------------------


def _governance_prompt_text() -> str:
    return (
        "# Implementation Prompt — GH-9601\n\n"
        "## Governance constraints\n\n"
        "Do not do any of the following unless a human reviewer explicitly asks:\n\n"
        "- create commits\n"
        "- push\n"
        "- merge\n"
        "- create PRs\n"
        "- approve, reject, or mark work finally complete\n"
        "- delete branches or worktrees\n"
        "- run destructive cleanup\n"
    )


def _opencode_events_with_governance_in_read_output() -> str:
    # A read tool returns file content that quotes governance text. Model
    # reasoning text repeats the prohibitions. No actual shell command runs
    # any of these actions.
    lines = [
        json.dumps({
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "/repo/AGENTS.md"},
                    "output": (
                        "Do not push, merge, approve, or run git push --force. "
                        "Do not delete branches or worktrees. "
                        "Do not approve task work without human review."
                    ),
                },
            },
        }),
        json.dumps({
            "type": "assistant_message",
            "part": {
                "type": "text",
                "text": (
                    "I will not git push or approve task. I will only modify "
                    "docs/tests as instructed."
                ),
            },
        }),
        json.dumps({
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "git status"},
                    "output": "On branch task/GH-9601",
                },
            },
        }),
    ]
    return "\n".join(lines) + "\n"


def _opencode_events_with_actual_executed_push() -> str:
    lines = [
        json.dumps({
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "git push origin HEAD:task/GH-9601"},
                    "output": "",
                },
            },
        }),
    ]
    return "\n".join(lines) + "\n"


class PolicyInstructionArtifactExemptionTests(unittest.TestCase):
    """Instruction/spec artifacts must not trigger suspicious-action scan."""

    def _run_with_artifact(self, filename: str, content: str) -> object:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            ctx = make_context(tmp)
            write_contract(ctx.artifact_dir, make_contract())
            (ctx.artifact_dir / filename).write_text(content, encoding="utf-8")
            return PolicyCheckValidator().run(ctx)

    def test_implementation_prompt_governance_text_does_not_fail(self) -> None:
        result = self._run_with_artifact(
            "implementation_prompt.md",
            _governance_prompt_text(),
        )
        self.assertEqual(result.status, "passed", result.summary)

    def test_task_execution_package_metadata_does_not_fail(self) -> None:
        payload = {
            "schema_version": "task_execution_package.v1",
            "task_key": "GH-9601",
            "implementation_prompt_path": "/x/implementation_prompt.md",
            "source_evidence": {
                "issue_spec_artifact_path": "/x/issue_spec.md",
            },
            "safety": {
                "branch_pushed": False,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
            },
            "notes": "Do not push, merge, approve, or delete branches.",
        }
        result = self._run_with_artifact(
            "task_execution_package.json",
            json.dumps(payload, indent=2),
        )
        self.assertEqual(result.status, "passed", result.summary)

    def test_issue_spec_with_forbidden_phrases_does_not_fail(self) -> None:
        body = (
            "# GitHub Issue Spec\n\n"
            "- Title: Do not push, do not merge\n\n"
            "## Body\n\n"
            "Operator must approve task before any git push or git merge.\n"
        )
        result = self._run_with_artifact("issue_spec.md", body)
        self.assertEqual(result.status, "passed", result.summary)


class PolicyExecutorEventLogTests(unittest.TestCase):
    """opencode-events.jsonl: scan executed shell commands, not reasoning."""

    def _run_with_event_log(self, content: str) -> object:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            ctx = make_context(tmp)
            write_contract(ctx.artifact_dir, make_contract())
            (ctx.artifact_dir / "opencode-events.jsonl").write_text(
                content, encoding="utf-8",
            )
            return PolicyCheckValidator().run(ctx)

    def test_reasoning_and_read_output_with_forbidden_phrases_passes(self) -> None:
        result = self._run_with_event_log(
            _opencode_events_with_governance_in_read_output()
        )
        self.assertEqual(result.status, "passed", result.summary)

    def test_executed_git_push_command_still_fails(self) -> None:
        result = self._run_with_event_log(
            _opencode_events_with_actual_executed_push()
        )
        self.assertEqual(result.status, "failed")
        self.assertIn("suspicious action", result.summary)


class PolicyChangedFileStillScannedTests(unittest.TestCase):
    """Actual executor-written files outside the exempt set are still scanned."""

    def _run_with_artifact(self, filename: str, content: str) -> object:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            ctx = make_context(tmp)
            write_contract(ctx.artifact_dir, make_contract())
            (ctx.artifact_dir / filename).write_text(content, encoding="utf-8")
            return PolicyCheckValidator().run(ctx)

    def test_diff_introducing_git_push_still_fails(self) -> None:
        diff = (
            "diff --git a/scripts/danger.sh b/scripts/danger.sh\n"
            "+++ b/scripts/danger.sh\n"
            "+git push --force origin main\n"
        )
        result = self._run_with_artifact("diff-after-opencode.patch", diff)
        self.assertEqual(result.status, "failed")
        self.assertIn("suspicious action", result.summary)

    def test_executor_written_script_with_gh_pr_merge_still_fails(self) -> None:
        script = (
            "#!/bin/bash\n"
            "gh pr merge 123 --merge --auto\n"
        )
        result = self._run_with_artifact("auto_merge.sh", script)
        self.assertEqual(result.status, "failed")
        self.assertIn("suspicious action", result.summary)

    def test_executor_written_script_with_branch_delete_still_fails(self) -> None:
        script = (
            "#!/bin/bash\n"
            "# Cleanup completed; delete worktree and delete branch\n"
        )
        result = self._run_with_artifact("cleanup.sh", script)
        self.assertEqual(result.status, "failed")
        self.assertIn("suspicious action", result.summary)


class PolicyDocsTestsGovernanceProseTests(unittest.TestCase):
    """A docs/tests-only changed-file set with governance prose should pass."""

    def test_docs_and_test_prose_in_diff_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            ctx = make_context(tmp)
            write_contract(ctx.artifact_dir, make_contract())

            # Captured executor diff: only docs/tests changed; the diff body
            # contains governance prose that names forbidden actions as
            # prohibitions but introduces no executable code that performs
            # them.
            diff = (
                "diff --git a/docs/p2-architecture-checkpoint.md b/docs/p2-architecture-checkpoint.md\n"
                "+++ b/docs/p2-architecture-checkpoint.md\n"
                "+- **no auto-push** — branch push requires explicit confirm\n"
                "+- **no auto-PR** — draft PR creation requires explicit confirm\n"
                "+- **no auto-merge** — merge is a manual GitHub-side action\n"
                "+- **no auto-cleanup** — cleanup requires explicit confirm\n"
                "diff --git a/tests/test_doc.py b/tests/test_doc.py\n"
                "+++ b/tests/test_doc.py\n"
                "+    self.assertIn(\"no auto-push\", content)\n"
                "+    self.assertIn(\"no auto-merge\", content)\n"
            )
            (ctx.artifact_dir / "diff-after-opencode.patch").write_text(
                diff, encoding="utf-8",
            )
            (ctx.artifact_dir / "git-status-after-opencode.txt").write_text(
                " M docs/p2-architecture-checkpoint.md\n"
                "?? tests/test_doc.py\n",
                encoding="utf-8",
            )

            result = PolicyCheckValidator().run(ctx)
            self.assertEqual(result.status, "passed", result.summary)


class PolicyPhase6E3RegressionTests(unittest.TestCase):
    """Reproduce the exact Phase 6E+3 false-positive bundle and assert pass."""

    def test_full_phase_6e3_evidence_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            ctx = make_context(tmp)
            write_contract(ctx.artifact_dir, make_contract())

            (ctx.artifact_dir / "implementation_prompt.md").write_text(
                _governance_prompt_text(), encoding="utf-8",
            )
            (ctx.artifact_dir / "issue_spec.md").write_text(
                "# GitHub Issue Spec\n\n"
                "- Title: Self-dogfood note\n\n"
                "## Body\n\n"
                "Do not push, do not merge, do not approve task.\n",
                encoding="utf-8",
            )
            (ctx.artifact_dir / "task_execution_package.json").write_text(
                json.dumps({
                    "schema_version": "task_execution_package.v1",
                    "task_key": "GH-9601",
                    "notes": "Do not push, merge, or delete branches.",
                }, indent=2),
                encoding="utf-8",
            )
            (ctx.artifact_dir / "opencode-events.jsonl").write_text(
                _opencode_events_with_governance_in_read_output(),
                encoding="utf-8",
            )
            (ctx.artifact_dir / "git-status-after-opencode.txt").write_text(
                " M docs/p2-architecture-checkpoint.md\n"
                "?? tests/test_local_self_dogfood_chain_doc.py\n",
                encoding="utf-8",
            )
            (ctx.artifact_dir / "diff-after-opencode.patch").write_text(
                "diff --git a/docs/p2-architecture-checkpoint.md b/docs/p2-architecture-checkpoint.md\n"
                "+++ b/docs/p2-architecture-checkpoint.md\n"
                "+- **no auto-push** — explicit confirm required\n"
                "+- **no auto-merge** — explicit confirm required\n",
                encoding="utf-8",
            )

            result = PolicyCheckValidator().run(ctx)
            self.assertEqual(result.status, "passed", result.summary)


# ----------------------------------------------------------------------
# Registry tests
# ----------------------------------------------------------------------


class PolicyRegistryTests(unittest.TestCase):
    def test_registry_lists_policy(self) -> None:
        from agent_taskflow.validators.registry import list_validator_names
        self.assertIn("policy", list_validator_names())

    def test_registry_returns_policy_validator(self) -> None:
        from agent_taskflow.validators.registry import get_validator
        v = get_validator("policy")
        self.assertIsInstance(v, PolicyCheckValidator)

    def test_registry_returns_policy_validator_with_scan_disabled(self) -> None:
        from agent_taskflow.validators.registry import get_validator
        v = get_validator("policy", scan_artifacts=False)
        self.assertIsInstance(v, PolicyCheckValidator)
        self.assertFalse(v.scan_artifacts)


if __name__ == "__main__":
    unittest.main()