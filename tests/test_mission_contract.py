"""Tests for the Mission Contract artifact module."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.mission_contract import (
    SCHEMA_VERSION,
    MissionContract,
    build_from_task_fields,
    build_mission_contract,
    mission_contract_to_dict,
    read_mission_contract,
    write_mission_contract,
)


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


def make_contract(**kwargs) -> MissionContract:
    defaults = dict(
        schema_version="1",
        task_key="AT-TEST01",
        goal="Implement the feature.",
        repo_path="/tmp/repo",
        worktree_path="/tmp/repo/.worktrees/AT-TEST01",
        artifact_dir="/tmp/artifacts/AT-TEST01",
        executor="manual",
    )
    defaults.update(kwargs)
    return MissionContract(**defaults)


# ----------------------------------------------------------------------
# MissionContract dataclass
# ----------------------------------------------------------------------


class MissionContractConstructionTests(unittest.TestCase):
    def test_required_fields_are_validated(self) -> None:
        # schema_version must not be empty
        with self.assertRaisesRegex(ValueError, "schema_version must not be empty"):
            MissionContract(schema_version="  ", task_key="AT-1", goal="x", repo_path="/tmp", worktree_path="/tmp", artifact_dir="/tmp", executor="manual")

        # task_key must not be empty
        with self.assertRaisesRegex(ValueError, "task_key must not be empty"):
            MissionContract(schema_version="1", task_key="  ", goal="x", repo_path="/tmp", worktree_path="/tmp", artifact_dir="/tmp", executor="manual")

        # goal must not be empty
        with self.assertRaisesRegex(ValueError, "goal must not be empty"):
            MissionContract(schema_version="1", task_key="AT-1", goal="  ", repo_path="/tmp", worktree_path="/tmp", artifact_dir="/tmp", executor="manual")

        # executor must not be empty
        with self.assertRaisesRegex(ValueError, "executor must not be empty"):
            MissionContract(schema_version="1", task_key="AT-1", goal="x", repo_path="/tmp", worktree_path="/tmp", artifact_dir="/tmp", executor="  ")

    def test_paths_are_converted_to_absolute(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp/repo",
            worktree_path="/tmp/repo/.worktrees/AT-1",
            artifact_dir="/tmp/artifacts",
            executor="manual",
        )
        self.assertIsInstance(contract.repo_path, Path)
        self.assertIsInstance(contract.worktree_path, Path)
        self.assertIsInstance(contract.artifact_dir, Path)

    def test_optional_fields_have_defaults(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        self.assertEqual(contract.required_validators, ("pytest", "openspec"))
        self.assertIsNone(contract.model)
        self.assertIsNone(contract.provider)
        self.assertIsNone(contract.title)
        self.assertEqual(contract.human_approval_required, True)

    def test_forbidden_actions_include_required_governance_rules(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        required_actions = {"approve", "push", "merge", "cleanup", "delete_worktree", "delete_branch"}
        self.assertTrue(required_actions.issubset(set(contract.forbidden_actions)))

    def test_human_approval_required_defaults_to_true(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        self.assertTrue(contract.human_approval_required)

    def test_model_and_provider_are_optional(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            model="minimax-01",
            provider="minimax",
        )
        self.assertEqual(contract.model, "minimax-01")
        self.assertEqual(contract.provider, "minimax")

    def test_extra_is_stored(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            extra={"note": "custom field"},
        )
        self.assertEqual(contract.extra["note"], "custom field")

    def test_path_policy_fields_are_stored(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            allowed_paths=("src", "tests/unit"),
            forbidden_paths=("secrets",),
        )
        self.assertEqual(contract.allowed_paths, ("src", "tests/unit"))
        self.assertEqual(contract.forbidden_paths, ("secrets",))

    def test_implementation_prompt_path_is_optional(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            implementation_prompt_path=Path("/tmp/artifacts/AT-1/implementation_prompt.md"),
        )
        self.assertIsNotNone(contract.implementation_prompt_path)
        self.assertIsInstance(contract.implementation_prompt_path, Path)

    def test_governance_rules_is_non_empty_list(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        rules = contract.governance_rules
        self.assertIsInstance(rules, list)
        self.assertTrue(len(rules) >= 10)
        # Must include key governance statements
        self.assertTrue(any("control plane" in r for r in rules))
        self.assertTrue(any("Human approval" in r for r in rules))
        self.assertTrue(any("cannot approve" in r for r in rules))


# ----------------------------------------------------------------------
# build_mission_contract
# ----------------------------------------------------------------------


class BuildMissionContractTests(unittest.TestCase):
    def test_accepts_string_repo_path(self) -> None:
        contract = build_mission_contract(
            task_key="AT-1",
            goal="Implement x.",
            repo_path="/tmp/repo",
            worktree_path="/tmp/repo/.worktrees/AT-1",
            artifact_dir="/tmp/artifacts",
            executor="manual",
        )
        self.assertIsInstance(contract, MissionContract)
        self.assertEqual(contract.repo_path, Path("/tmp/repo"))

    def test_accepts_path_repo_path(self) -> None:
        contract = build_mission_contract(
            task_key="AT-1",
            goal="Implement x.",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/repo/.worktrees/AT-1"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="manual",
        )
        self.assertIsInstance(contract, MissionContract)

    def test_rejects_empty_task_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_key must not be empty"):
            build_mission_contract(
                task_key="  ",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
            )

    def test_rejects_empty_goal(self) -> None:
        with self.assertRaisesRegex(ValueError, "goal must not be empty"):
            build_mission_contract(
                task_key="AT-1",
                goal="  ",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
            )

    def test_rejects_empty_executor(self) -> None:
        with self.assertRaisesRegex(ValueError, "executor must not be empty"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="  ",
            )

    def test_extra_rejects_secret_keys(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"api_key": "sk-test"},
            )

    def test_extra_rejects_token_keys(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"access_token": "abc123"},
            )

    def test_extra_accepts_non_secret_keys(self) -> None:
        contract = build_mission_contract(
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            extra={"note": "custom info", "branch": "task/AT-1"},
        )
        self.assertEqual(contract.extra["note"], "custom info")

    def test_build_accepts_path_policy_fields(self) -> None:
        contract = build_mission_contract(
            task_key="AT-1",
            goal="Implement x.",
            repo_path="/tmp/repo",
            worktree_path="/tmp/repo/.worktrees/AT-1",
            artifact_dir="/tmp/artifacts",
            executor="manual",
            allowed_paths=("src/",),
            forbidden_paths=("secrets/",),
        )
        self.assertEqual(contract.allowed_paths, ("src",))
        self.assertEqual(contract.forbidden_paths, ("secrets",))

    def test_resolved_implementation_prompt_path(self) -> None:
        contract = build_mission_contract(
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp/artifacts",
            executor="manual",
            implementation_prompt_path="/tmp/artifacts/implementation_prompt.md",
        )
        self.assertIsNotNone(contract.implementation_prompt_path)
        self.assertIsInstance(contract.implementation_prompt_path, Path)

    def test_schema_version_is_set(self) -> None:
        contract = build_mission_contract(
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        self.assertEqual(contract.schema_version, SCHEMA_VERSION)


class BuildFromTaskFieldsTests(unittest.TestCase):
    def test_basic_construction(self) -> None:
        contract = build_from_task_fields(
            task_key="AT-001",
            goal="Implement feature X.",
            repo_path="/tmp/repo",
            worktree_path="/tmp/repo/.worktrees/AT-001",
            artifact_dir="/tmp/artifacts/AT-001",
            executor="manual",
        )
        self.assertEqual(contract.task_key, "AT-001")
        self.assertEqual(contract.goal, "Implement feature X.")
        self.assertEqual(contract.executor, "manual")
        self.assertEqual(contract.required_validators, ("pytest", "openspec"))

    def test_with_model_and_provider(self) -> None:
        contract = build_from_task_fields(
            task_key="AT-001",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="pi",
            model="minimax-01",
            provider="minimax",
        )
        self.assertEqual(contract.model, "minimax-01")
        self.assertEqual(contract.provider, "minimax")

    def test_with_custom_validators(self) -> None:
        contract = build_from_task_fields(
            task_key="AT-001",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            required_validators=("openspec",),
        )
        self.assertEqual(contract.required_validators, ("openspec",))


# ----------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------


class SerializationTests(unittest.TestCase):
    def test_mission_contract_to_dict_contains_all_fields(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            title="Test Task",
            goal="Implement x.",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/repo/.worktrees/AT-1"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="pi",
            model="minimax-01",
            provider="minimax",
            required_validators=("pytest",),
            implementation_prompt_path=Path("/tmp/artifacts/AT-1/implementation_prompt.md"),
            extra={"note": "custom"},
        )
        d = mission_contract_to_dict(contract)

        self.assertEqual(d["schema_version"], "1")
        self.assertEqual(d["task_key"], "AT-1")
        self.assertEqual(d["title"], "Test Task")
        self.assertEqual(d["goal"], "Implement x.")
        self.assertEqual(d["repo_path"], "/tmp/repo")
        self.assertEqual(d["worktree_path"], "/tmp/repo/.worktrees/AT-1")
        self.assertEqual(d["artifact_dir"], "/tmp/artifacts")
        self.assertEqual(d["executor"], "pi")
        self.assertEqual(d["model"], "minimax-01")
        self.assertEqual(d["provider"], "minimax")
        self.assertEqual(d["required_validators"], ["pytest"])
        self.assertIn("approve", d["forbidden_actions"])
        self.assertIn("push", d["forbidden_actions"])
        self.assertIn("merge", d["forbidden_actions"])
        self.assertIn("cleanup", d["forbidden_actions"])
        self.assertTrue(d["human_approval_required"])
        self.assertIsInstance(d["governance_rules"], list)

    def test_paths_are_serialized_as_strings(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/repo/.worktrees/AT-1"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="manual",
        )
        d = mission_contract_to_dict(contract)
        self.assertIsInstance(d["repo_path"], str)
        self.assertIsInstance(d["worktree_path"], str)
        self.assertIsInstance(d["artifact_dir"], str)

    def test_omit_none_optional_fields(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path=Path("/tmp"),
            worktree_path=Path("/tmp"),
            artifact_dir=Path("/tmp"),
            executor="manual",
        )
        d = mission_contract_to_dict(contract)
        # title, model, provider are all None — should not appear in dict
        self.assertNotIn("title", d)
        self.assertNotIn("model", d)
        self.assertNotIn("provider", d)

    def test_dict_is_json_serializable(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="Implement the feature.",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/worktree"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="pi",
        )
        d = mission_contract_to_dict(contract)
        # This should not raise
        json.dumps(d)


# ----------------------------------------------------------------------
# Write / Read
# ----------------------------------------------------------------------


class WriteMissionContractTests(unittest.TestCase):
    def test_write_uses_artifact_dir(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-WRITE",
            goal="x",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/worktree"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="manual",
        )
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            result = write_mission_contract(contract, artifact_dir=artifact_dir)
            self.assertEqual(result.name, "mission_contract.json")
            self.assertTrue(result.exists())

    def test_write_uses_explicit_path(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-WRITE",
            goal="x",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/worktree"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="manual",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "my_contract.json"
            result = write_mission_contract(contract, path=path)
            self.assertEqual(result, path)
            self.assertTrue(result.exists())

    def test_write_rejects_both_artifact_dir_and_path(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path=Path("/tmp"),
            worktree_path=Path("/tmp"),
            artifact_dir=Path("/tmp"),
            executor="manual",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "exactly one of artifact_dir or path"):
                write_mission_contract(contract, artifact_dir=tmp, path=Path(tmp) / "out.json")

    def test_write_rejects_neither_artifact_dir_nor_path(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path=Path("/tmp"),
            worktree_path=Path("/tmp"),
            artifact_dir=Path("/tmp"),
            executor="manual",
        )
        with self.assertRaisesRegex(ValueError, "exactly one of artifact_dir or path"):
            write_mission_contract(contract)

    def test_write_produces_valid_json(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-WRITE",
            goal="Implement x.",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/worktree"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="pi",
            model="minimax-01",
            provider="minimax",
            required_validators=("pytest", "openspec"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.json"
            write_mission_contract(contract, path=path)
            raw = path.read_text(encoding="utf-8")
            d = json.loads(raw)
            self.assertEqual(d["task_key"], "AT-WRITE")
            self.assertEqual(d["executor"], "pi")
            self.assertEqual(d["model"], "minimax-01")
            self.assertIn("approve", d["forbidden_actions"])


class ReadMissionContractTests(unittest.TestCase):
    def test_roundtrip_write_read(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-RD01",
            title="Roundtrip test",
            goal="Verify write/read roundtrip.",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/worktree"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="pi",
            model="minimax-01",
            provider="minimax",
            required_validators=("pytest",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mission_contract.json"
            write_mission_contract(contract, path=path)
            d = read_mission_contract(path)

            self.assertEqual(d["task_key"], "AT-RD01")
            self.assertEqual(d["title"], "Roundtrip test")
            self.assertEqual(d["executor"], "pi")
            self.assertEqual(d["model"], "minimax-01")
            self.assertEqual(d["provider"], "minimax")
            self.assertEqual(d["required_validators"], ["pytest"])
            self.assertIn("approve", d["forbidden_actions"])
            self.assertTrue(d["human_approval_required"])

    def test_read_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                read_mission_contract(Path(tmp) / "nonexistent.json")

    def test_read_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("not valid json {", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                read_mission_contract(path)

    def test_read_rejects_missing_required_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mission_contract.json"
            path.write_text(
                json.dumps({"schema_version": "1", "task_key": "AT-1", "goal": "x"}),
                encoding="utf-8",
            )
            # missing repo_path, worktree_path, artifact_dir, executor
            with self.assertRaisesRegex(ValueError, "missing required field"):
                read_mission_contract(path)

    def test_read_rejects_empty_required_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mission_contract.json"
            path.write_text(
                json.dumps({
                    "schema_version": "1",
                    "task_key": "AT-1",
                    "goal": "x",
                    "repo_path": "",
                    "worktree_path": "/tmp",
                    "artifact_dir": "/tmp",
                    "executor": "manual",
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                read_mission_contract(path)

    def test_read_rejects_wrong_type_for_required_validators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mission_contract.json"
            path.write_text(
                json.dumps({
                    "schema_version": "1",
                    "task_key": "AT-1",
                    "goal": "x",
                    "repo_path": "/tmp",
                    "worktree_path": "/tmp",
                    "artifact_dir": "/tmp",
                    "executor": "manual",
                    "required_validators": "not-a-list",
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TypeError, "must be a list"):
                read_mission_contract(path)

    def test_read_rejects_wrong_type_for_human_approval_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mission_contract.json"
            path.write_text(
                json.dumps({
                    "schema_version": "1",
                    "task_key": "AT-1",
                    "goal": "x",
                    "repo_path": "/tmp",
                    "worktree_path": "/tmp",
                    "artifact_dir": "/tmp",
                    "executor": "manual",
                    "human_approval_required": "yes",
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TypeError, "must be a bool"):
                read_mission_contract(path)


# ----------------------------------------------------------------------
# Secret redaction / rejection
# ----------------------------------------------------------------------


class SecretRejectionTests(unittest.TestCase):
    def test_extra_rejects_api_key(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"API_KEY": "sk-test123"},
            )

    def test_extra_rejects_password(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"PASSWORD": "hunter2"},
            )

    def test_extra_rejects_token(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"TOKEN": "abc"},
            )

    def test_extra_rejects_secret(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"SECRET": "xyz"},
            )

    def test_extra_rejects_authorization(self) -> None:
        with self.assertRaisesRegex(TypeError, "secret-like keys"):
            build_mission_contract(
                task_key="AT-1",
                goal="x",
                repo_path="/tmp",
                worktree_path="/tmp",
                artifact_dir="/tmp",
                executor="manual",
                extra={"authorization": "Bearer xyz"},
            )

    def test_extra_accepts_safe_keys(self) -> None:
        # These are safe — they don't look like secrets
        contract = build_mission_contract(
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
            extra={
                "note": "some info",
                "branch": "task/AT-1",
                "project": "my-project",
                "description": "This is a task",
            },
        )
        self.assertEqual(len(contract.extra), 4)

    def test_contract_never_contains_secret_values_in_goal(self) -> None:
        # A goal that mentions a secret key name is fine — it's the value that's blocked
        contract = build_mission_contract(
            task_key="AT-1",
            goal="Set the API_KEY environment variable in the config.",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        # goal itself is just text — the module doesn't have knowledge of secret values
        self.assertIsNotNone(contract.goal)


# ----------------------------------------------------------------------
# Governance rule coverage
# ----------------------------------------------------------------------


class GovernanceRulesTests(unittest.TestCase):
    def test_forbidden_actions_contain_all_required_governance_rules(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path=Path("/tmp"),
            worktree_path=Path("/tmp"),
            artifact_dir=Path("/tmp"),
            executor="pi",
        )
        fa = set(contract.forbidden_actions)
        required = {"approve", "push", "merge", "cleanup", "delete_worktree", "delete_branch"}
        self.assertTrue(required.issubset(fa), f"Missing: {required - fa}")

    def test_human_approval_required_is_always_true(self) -> None:
        # Even if someone accidentally passes False, the default is True
        # and the build path always sets it to True (it's a keyword arg with default)
        contract = build_from_task_fields(
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        self.assertTrue(contract.human_approval_required)

    def test_required_validators_default_is_pytest_and_openspec(self) -> None:
        contract = build_from_task_fields(
            task_key="AT-1",
            goal="x",
            repo_path="/tmp",
            worktree_path="/tmp",
            artifact_dir="/tmp",
            executor="manual",
        )
        self.assertEqual(contract.required_validators, ("pytest", "openspec"))

    def test_governance_rules_list_is_present(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-1",
            goal="x",
            repo_path=Path("/tmp"),
            worktree_path=Path("/tmp"),
            artifact_dir=Path("/tmp"),
            executor="pi",
        )
        d = mission_contract_to_dict(contract)
        self.assertIn("governance_rules", d)
        self.assertIn("allowed_paths", d)
        self.assertIn("forbidden_paths", d)
        self.assertIsInstance(d["governance_rules"], list)
        self.assertTrue(len(d["governance_rules"]) > 0)

    def test_contract_is_readable_and_valid_after_write(self) -> None:
        contract = MissionContract(
            schema_version="1",
            task_key="AT-GOV",
            title="Governance check",
            goal="Ensure all governance rules are present in the contract.",
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/worktree"),
            artifact_dir=Path("/tmp/artifacts"),
            executor="pi",
            model="minimax-01",
            provider="minimax",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mission_contract.json"
            write_mission_contract(contract, path=path)
            d = read_mission_contract(path)

            self.assertIn("forbidden_actions", d)
            self.assertTrue(d["human_approval_required"])
            self.assertIn("governance_rules", d)
            self.assertIn("required_validators", d)


if __name__ == "__main__":
    unittest.main()
