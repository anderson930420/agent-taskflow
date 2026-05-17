"""Tests for the changed-files path policy validator."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.validators.base import ValidatorContext
from agent_taskflow.validators.changed_files import (
    AUDIT_ARTIFACT_NAME,
    ChangedFilesValidator,
)
from agent_taskflow.validators.registry import get_validator, list_validator_names


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=True,
    )


class ChangedFilesValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.artifact_dir = self.root / "artifacts" / "AT-CF01"
        self.repo.mkdir()
        self.artifact_dir.mkdir(parents=True)
        _run_git(self.repo, "init")
        _run_git(self.repo, "config", "user.email", "test@example.invalid")
        _run_git(self.repo, "config", "user.name", "Test User")

        (self.repo / "src").mkdir()
        (self.repo / "docs").mkdir()
        (self.repo / "config").mkdir()
        (self.repo / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
        (self.repo / "config" / "secret.yml").write_text("placeholder\n", encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_contract(
        self,
        *,
        allowed_paths: tuple[str, ...] = (),
        forbidden_paths: tuple[str, ...] = (),
    ) -> None:
        contract = build_mission_contract(
            task_key="AT-CF01",
            goal="Changed files validator test.",
            repo_path=self.repo,
            worktree_path=self.repo,
            artifact_dir=self.artifact_dir,
            executor="manual",
            required_validators=("changed-files",),
            allowed_paths=allowed_paths,
            forbidden_paths=forbidden_paths,
        )
        write_mission_contract(contract, artifact_dir=self.artifact_dir)

    def write_raw_contract(
        self,
        *,
        allowed_paths: object,
        forbidden_paths: object = (),
    ) -> None:
        contract = {
            "schema_version": "1",
            "task_key": "AT-CF01",
            "goal": "Changed files validator test.",
            "repo_path": str(self.repo),
            "worktree_path": str(self.repo),
            "artifact_dir": str(self.artifact_dir),
            "executor": "manual",
            "required_validators": ["changed-files"],
            "forbidden_actions": ["push", "merge", "cleanup"],
            "expected_artifacts": ["changed-files-audit.json"],
            "allowed_paths": allowed_paths,
            "forbidden_paths": forbidden_paths,
            "human_approval_required": True,
            "governance_rules": [],
        }
        (self.artifact_dir / "mission_contract.json").write_text(
            json.dumps(contract),
            encoding="utf-8",
        )

    def make_context(self) -> ValidatorContext:
        return ValidatorContext(
            task_key="AT-CF01",
            project="agent-taskflow",
            worktree_path=self.repo,
            artifact_dir=self.artifact_dir,
        )

    def read_audit(self) -> dict:
        return json.loads((self.artifact_dir / AUDIT_ARTIFACT_NAME).read_text(encoding="utf-8"))

    def test_allowed_changed_file_passes(self) -> None:
        self.write_contract(allowed_paths=("src",), forbidden_paths=("config",))
        (self.repo / "src" / "app.py").write_text("print('updated')\n", encoding="utf-8")

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "passed")
        audit = self.read_audit()
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["violations"], [])
        self.assertEqual(audit["changed_files"][0]["path"], "src/app.py")

    def test_forbidden_changed_file_blocks(self) -> None:
        self.write_contract(allowed_paths=("src",), forbidden_paths=("config",))
        (self.repo / "config" / "secret.yml").write_text("changed\n", encoding="utf-8")

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "failed")
        audit = self.read_audit()
        self.assertEqual(audit["violations"][0]["path"], "config/secret.yml")
        self.assertEqual(audit["violations"][0]["reason"], "forbidden_path")

    def test_untracked_unexpected_file_blocks(self) -> None:
        self.write_contract(allowed_paths=("src",), forbidden_paths=("config",))
        (self.repo / "scratch.txt").write_text("unexpected\n", encoding="utf-8")

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "failed")
        audit = self.read_audit()
        self.assertEqual(audit["violations"][0]["path"], "scratch.txt")
        self.assertEqual(audit["violations"][0]["reason"], "outside_allowed_paths")
        self.assertEqual(audit["changed_files"][0]["status"], "??")

    def test_deleted_forbidden_file_blocks(self) -> None:
        self.write_contract(allowed_paths=("src",), forbidden_paths=("config",))
        (self.repo / "config" / "secret.yml").unlink()

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "failed")
        audit = self.read_audit()
        self.assertEqual(audit["violations"][0]["path"], "config/secret.yml")
        self.assertEqual(audit["violations"][0]["reason"], "forbidden_path")
        self.assertIn("D", audit["changed_files"][0]["status"])

    def test_no_changes_produces_clean_audit(self) -> None:
        self.write_contract(allowed_paths=("src",), forbidden_paths=("config",))

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "passed")
        audit = self.read_audit()
        self.assertEqual(audit["changed_files"], [])
        self.assertEqual(audit["violations"], [])
        self.assertEqual(audit["status"], "passed")

    def test_malformed_allowed_paths_non_string_blocks(self) -> None:
        self.write_raw_contract(allowed_paths=["src", 123])

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "blocked")
        self.assertIn("Malformed path policy", result.summary)

    def test_malformed_allowed_paths_empty_string_blocks(self) -> None:
        self.write_raw_contract(allowed_paths=[""])

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "blocked")
        self.assertIn("Malformed path policy", result.summary)

    def test_malformed_allowed_paths_absolute_path_blocks(self) -> None:
        self.write_raw_contract(allowed_paths=["/tmp/src"])

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "blocked")
        self.assertIn("Malformed path policy", result.summary)

    def test_malformed_allowed_paths_traversal_blocks(self) -> None:
        self.write_raw_contract(allowed_paths=["../src"])

        result = ChangedFilesValidator().run(self.make_context())

        self.assertEqual(result.status, "blocked")
        self.assertIn("Malformed path policy", result.summary)

    def test_registry_includes_changed_files_validator(self) -> None:
        self.assertIn("changed-files", list_validator_names())
        self.assertIsInstance(get_validator("changed-files"), ChangedFilesValidator)


if __name__ == "__main__":
    unittest.main()
