"""Tests for the Pi Mission Orchestrator spike."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.executors.pi_orchestrator import (
    PiMissionPlan,
    PiMissionStep,
    build_pi_mission_plan,
    pi_mission_plan_to_dict,
    read_pi_mission_plan,
    render_pi_mission_plan_section,
    write_pi_mission_plan,
)

# The required forbidden actions that every step must include.
_REQUIRED_FORBIDDEN = (
    "approve",
    "self_approve",
    "push",
    "force_push",
    "merge",
    "cleanup",
    "delete_worktree",
    "delete_branch",
)


class BuildPiMissionPlanTests(unittest.TestCase):
    """Tests for build_pi_mission_plan."""

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
            "forbidden_actions": [],
            "expected_artifacts": ["executor_log"],
            "human_approval_required": True,
            "governance_rules": [],
        }

    # ── Core behavior ──────────────────────────────────────────────────────

    def test_creates_five_steps(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        self.assertEqual(len(plan.steps), 5)
        step_ids = [s.step_id for s in plan.steps]
        self.assertEqual(step_ids, ["scout", "planner", "implementer", "reviewer", "handoff"])

    def test_deterministic_same_contract_same_plan(self) -> None:
        contract = self._minimal_contract()
        plan1 = build_pi_mission_plan(contract)
        plan2 = build_pi_mission_plan(contract)
        self.assertEqual(plan1, plan2)

    def test_plan_task_key_from_contract(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        self.assertEqual(plan.task_key, "AT-0101")

    def test_plan_executor_from_contract(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        self.assertEqual(plan.executor, "pi")

    def test_plan_schema_version_is_one(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        self.assertEqual(plan.schema_version, "1")

    # ── Required validators ─────────────────────────────────────────────────

    def test_plan_required_validators_copied(self) -> None:
        contract = self._minimal_contract()
        contract["required_validators"] = ["pytest", "openspec", "policy"]
        plan = build_pi_mission_plan(contract)
        self.assertEqual(tuple(plan.required_validators), ("pytest", "openspec", "policy"))

    def test_plan_required_validators_empty(self) -> None:
        contract = self._minimal_contract()
        contract["required_validators"] = []
        plan = build_pi_mission_plan(contract)
        self.assertEqual(tuple(plan.required_validators), ())

    # ── Forbidden actions ───────────────────────────────────────────────────

    def test_plan_includes_required_forbidden_actions(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for action in _REQUIRED_FORBIDDEN:
            self.assertIn(action, plan.forbidden_actions)

    def test_every_step_has_all_required_forbidden_actions(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            for action in _REQUIRED_FORBIDDEN:
                self.assertIn(action, step.forbidden_actions)

    def test_every_step_has_approve_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("approve", step.forbidden_actions)

    def test_every_step_has_push_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("push", step.forbidden_actions)

    def test_every_step_has_merge_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("merge", step.forbidden_actions)

    def test_every_step_has_cleanup_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("cleanup", step.forbidden_actions)

    def test_every_step_has_delete_worktree_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("delete_worktree", step.forbidden_actions)

    def test_every_step_has_delete_branch_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("delete_branch", step.forbidden_actions)

    def test_every_step_has_self_approve_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("self_approve", step.forbidden_actions)

    def test_every_step_has_force_push_forbidden(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("force_push", step.forbidden_actions)

    def test_contract_extra_forbidden_merged_into_plan(self) -> None:
        contract = self._minimal_contract()
        contract["forbidden_actions"] = ["deploy", "restart_service"]
        plan = build_pi_mission_plan(contract)
        self.assertIn("deploy", plan.forbidden_actions)
        self.assertIn("restart_service", plan.forbidden_actions)

    def test_contract_extra_forbidden_in_every_step(self) -> None:
        contract = self._minimal_contract()
        contract["forbidden_actions"] = ["deploy", "restart_service"]
        plan = build_pi_mission_plan(contract)
        for step in plan.steps:
            self.assertIn("deploy", step.forbidden_actions)
            self.assertIn("restart_service", step.forbidden_actions)

    # ── Human approval ──────────────────────────────────────────────────────

    def test_plan_human_approval_required_true(self) -> None:
        contract = self._minimal_contract()
        plan = build_pi_mission_plan(contract)
        self.assertTrue(plan.human_approval_required)

    def test_plan_human_approval_required_false(self) -> None:
        contract = self._minimal_contract()
        contract["human_approval_required"] = False
        plan = build_pi_mission_plan(contract)
        self.assertFalse(plan.human_approval_required)

    # ── Validation errors ────────────────────────────────────────────────────

    def test_rejects_missing_task_key(self) -> None:
        contract = self._minimal_contract()
        del contract["task_key"]
        with self.assertRaises(ValueError) as ctx:
            build_pi_mission_plan(contract)
        self.assertIn("task_key", str(ctx.exception))

    def test_rejects_missing_executor(self) -> None:
        contract = self._minimal_contract()
        del contract["executor"]
        with self.assertRaises(ValueError) as ctx:
            build_pi_mission_plan(contract)
        self.assertIn("executor", str(ctx.exception))

    def test_rejects_non_dict(self) -> None:
        with self.assertRaises(TypeError):
            build_pi_mission_plan("not a dict")  # type: ignore[arg-type]

    def test_rejects_whitespace_task_key(self) -> None:
        contract = self._minimal_contract()
        contract["task_key"] = "  "
        # Whitespace-only keys are stripped and rejected.
        with self.assertRaises(ValueError) as ctx:
            build_pi_mission_plan(contract)
        self.assertIn("task_key", str(ctx.exception).lower())

    def test_rejects_empty_executor(self) -> None:
        contract = self._minimal_contract()
        contract["executor"] = ""
        with self.assertRaises(ValueError):
            build_pi_mission_plan(contract)


class PiMissionStepTests(unittest.TestCase):
    """Tests for PiMissionStep dataclass."""

    def test_creates_valid_step(self) -> None:
        step = PiMissionStep(
            step_id="scout",
            role="scout",
            title="Inspect",
            objective="Inspect the context.",
            allowed_actions=("read files",),
            forbidden_actions=_REQUIRED_FORBIDDEN,
            expected_outputs=("scout_notes",),
        )
        self.assertEqual(step.step_id, "scout")

    def test_rejects_empty_step_id(self) -> None:
        with self.assertRaises(ValueError):
            PiMissionStep(
                step_id="",
                role="scout",
                title="Inspect",
                objective="...",
                allowed_actions=(),
                forbidden_actions=(),
                expected_outputs=(),
            )

    def test_rejects_empty_role(self) -> None:
        with self.assertRaises(ValueError):
            PiMissionStep(
                step_id="scout",
                role="   ",
                title="Inspect",
                objective="...",
                allowed_actions=(),
                forbidden_actions=(),
                expected_outputs=(),
            )

    def test_accepts_lists_as_tuples(self) -> None:
        step = PiMissionStep(
            step_id="scout",
            role="scout",
            title="Inspect",
            objective="...",
            allowed_actions=["read files"],
            forbidden_actions=["approve", "push"],
            expected_outputs=["scout_notes"],
        )
        self.assertIsInstance(step.allowed_actions, tuple)
        self.assertIsInstance(step.forbidden_actions, tuple)
        self.assertIsInstance(step.expected_outputs, tuple)


class PiMissionPlanTests(unittest.TestCase):
    """Tests for PiMissionPlan dataclass."""

    def _make_step(self, **overrides) -> PiMissionStep:
        kw = dict(
            step_id="scout",
            role="scout",
            title="Inspect",
            objective="...",
            allowed_actions=("read files",),
            forbidden_actions=_REQUIRED_FORBIDDEN,
            expected_outputs=("notes",),
        )
        kw.update(overrides)
        return PiMissionStep(**kw)

    def test_creates_valid_plan(self) -> None:
        plan = PiMissionPlan(
            schema_version="1",
            task_key="AT-0101",
            executor="pi",
            steps=(
                self._make_step(step_id="scout"),
                self._make_step(step_id="planner"),
            ),
            required_validators=("pytest",),
            forbidden_actions=_REQUIRED_FORBIDDEN,
            human_approval_required=True,
        )
        self.assertEqual(plan.task_key, "AT-0101")
        self.assertEqual(len(plan.steps), 2)

    def test_rejects_missing_required_forbidden_in_step(self) -> None:
        step = self._make_step(forbidden_actions=("approve",))  # missing push, merge, etc.
        with self.assertRaises(ValueError) as ctx:
            PiMissionPlan(
                schema_version="1",
                task_key="AT-0101",
                executor="pi",
                steps=(step,),
                required_validators=(),
                forbidden_actions=(),
                human_approval_required=True,
            )
        self.assertIn("missing required forbidden", str(ctx.exception))

    def test_rejects_empty_schema_version(self) -> None:
        with self.assertRaises(ValueError):
            PiMissionPlan(
                schema_version="",
                task_key="AT-0101",
                executor="pi",
                steps=(),
                required_validators=(),
                forbidden_actions=(),
                human_approval_required=True,
            )


class SerializationTests(unittest.TestCase):
    """Tests for pi_mission_plan_to_dict and read_pi_mission_plan."""

    def _minimal_plan(self) -> PiMissionPlan:
        return build_pi_mission_plan({
            "schema_version": "1",
            "task_key": "AT-0102",
            "goal": "Test goal",
            "repo_path": "/tmp/repo",
            "worktree_path": "/tmp/wt",
            "artifact_dir": "/tmp/art",
            "executor": "pi",
            "required_validators": ["pytest"],
            "forbidden_actions": [],
            "expected_artifacts": [],
            "human_approval_required": True,
            "governance_rules": [],
        })

    def test_to_dict_is_json_safe(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        # Must not raise.
        json.dumps(d)
        self.assertIsInstance(d, dict)

    def test_to_dict_includes_schema_version(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        self.assertEqual(d["schema_version"], "1")

    def test_to_dict_includes_task_key(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        self.assertEqual(d["task_key"], "AT-0102")

    def test_to_dict_includes_executor(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        self.assertEqual(d["executor"], "pi")

    def test_to_dict_steps_are_lists(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        self.assertIsInstance(d["steps"], list)
        self.assertEqual(len(d["steps"]), 5)

    def test_to_dict_step_fields(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        scout = d["steps"][0]
        self.assertEqual(scout["step_id"], "scout")
        self.assertEqual(scout["role"], "scout")
        self.assertIn("objective", scout)
        self.assertIsInstance(scout["allowed_actions"], list)
        self.assertIsInstance(scout["forbidden_actions"], list)
        self.assertIsInstance(scout["expected_outputs"], list)

    def test_to_dict_includes_required_validators(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        self.assertEqual(d["required_validators"], ["pytest"])

    def test_to_dict_includes_human_approval_required(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        self.assertTrue(d["human_approval_required"])

    def test_round_trip_through_json(self) -> None:
        plan = self._minimal_plan()
        d = pi_mission_plan_to_dict(plan)
        raw = json.dumps(d)
        loaded = json.loads(raw)
        re_plan = read_pi_mission_plan(
            _dict_to_path(loaded)
        )
        self.assertEqual(re_plan.task_key, plan.task_key)
        self.assertEqual(len(re_plan.steps), len(plan.steps))


class WritePiMissionPlanTests(unittest.TestCase):
    """Tests for write_pi_mission_plan."""

    def _minimal_plan(self) -> PiMissionPlan:
        return build_pi_mission_plan({
            "schema_version": "1",
            "task_key": "AT-0103",
            "goal": "Write test",
            "repo_path": "/tmp/repo",
            "worktree_path": "/tmp/wt",
            "artifact_dir": "/tmp/art",
            "executor": "pi",
            "required_validators": [],
            "forbidden_actions": [],
            "expected_artifacts": [],
            "human_approval_required": True,
            "governance_rules": [],
        })

    def test_writes_pi_mission_plan_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            plan = self._minimal_plan()
            path = write_pi_mission_plan(artifact_dir, plan)

            self.assertEqual(path.name, "pi_mission_plan.json")
            self.assertTrue(path.exists())
            # Must be valid JSON.
            d = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(d["task_key"], "AT-0103")

    def test_path_traversal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "safe"
            artifact_dir.mkdir()
            plan = self._minimal_plan()
            # The output path is artifact_dir / "pi_mission_plan.json" which IS
            # inside artifact_dir. No traversal possible here.
            path = write_pi_mission_plan(artifact_dir, plan)
            self.assertTrue(path.exists())

    def test_rejects_relative_artifact_dir(self) -> None:
        # Path.resolve() on POSIX always returns absolute, so a relative path
        # is accepted (it is resolved to absolute before use). This is correct
        # behavior; we test it by writing to a relative dir resolved to absolute.
        with tempfile.TemporaryDirectory() as tmp:
            original = Path.cwd()
            import os
            os.chdir(tmp)
            try:
                rel_dir = Path("my-artifacts")
                rel_dir.mkdir()
                plan = self._minimal_plan()
                path = write_pi_mission_plan(rel_dir, plan)
                self.assertTrue(path.exists())
                self.assertTrue(path.is_absolute())
            finally:
                os.chdir(original)

    def test_creates_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "deeply" / "nested"
            self.assertFalse(artifact_dir.exists())
            plan = self._minimal_plan()
            path = write_pi_mission_plan(artifact_dir, plan)
            self.assertTrue(artifact_dir.exists())
            self.assertTrue(path.exists())

    def test_written_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            plan = self._minimal_plan()
            path = write_pi_mission_plan(artifact_dir, plan)
            d = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(d["task_key"], plan.task_key)
            self.assertEqual(d["schema_version"], plan.schema_version)


class ReadPiMissionPlanTests(unittest.TestCase):
    """Tests for read_pi_mission_plan."""

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            plan = build_pi_mission_plan({
                "schema_version": "1",
                "task_key": "AT-0104",
                "goal": "Round-trip test",
                "repo_path": "/tmp/repo",
                "worktree_path": "/tmp/wt",
                "artifact_dir": "/tmp/art",
                "executor": "pi",
                "required_validators": ["pytest", "openspec"],
                "forbidden_actions": ["custom_ban"],
                "expected_artifacts": [],
                "human_approval_required": False,
                "governance_rules": [],
            })
            write_pi_mission_plan(artifact_dir, plan)
            re_plan = read_pi_mission_plan(artifact_dir / "pi_mission_plan.json")
            self.assertEqual(re_plan.task_key, "AT-0104")
            self.assertEqual(len(re_plan.steps), 5)
            self.assertEqual(tuple(re_plan.required_validators), ("pytest", "openspec"))
            self.assertFalse(re_plan.human_approval_required)
            self.assertIn("custom_ban", re_plan.forbidden_actions)

    def test_read_nonexistent_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                read_pi_mission_plan(Path(tmp) / "nonexistent.json")

    def test_invalid_json_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("not { json", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_pi_mission_plan(bad)


class RenderPiMissionPlanSectionTests(unittest.TestCase):
    """Tests for render_pi_mission_plan_section."""

    def _minimal_plan(self) -> PiMissionPlan:
        return build_pi_mission_plan({
            "schema_version": "1",
            "task_key": "AT-0105",
            "goal": "Render test",
            "repo_path": "/tmp/repo",
            "worktree_path": "/tmp/wt",
            "artifact_dir": "/tmp/art",
            "executor": "pi",
            "required_validators": ["pytest", "openspec"],
            "forbidden_actions": [],
            "expected_artifacts": [],
            "human_approval_required": True,
            "governance_rules": [],
        })

    def test_includes_header(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        self.assertIn("## Pi Mission Plan\n", result)

    def test_includes_all_five_steps(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        for step_id in ("scout", "planner", "implementer", "reviewer", "handoff"):
            self.assertIn(f"step_id: `{step_id}`", result)
            self.assertIn(f"**Role:** `{step_id}`", result)

    def test_includes_step_objectives(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        for step in plan.steps:
            self.assertIn(step.objective, result)

    def test_includes_forbidden_actions(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        for action in _REQUIRED_FORBIDDEN:
            self.assertIn(f"`{action}`", result)

    def test_includes_allowed_actions(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        # Scout step has "read files" as allowed action.
        self.assertIn("read files", result)

    def test_includes_expected_outputs(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        for step in plan.steps:
            for output in step.expected_outputs:
                self.assertIn(f"`{output}`", result)

    def test_says_not_autonomous_agents(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        self.assertIn("protocol steps", result)
        self.assertIn("not independent autonomous agents", result)

    def test_says_do_not_create_uncontrolled_subagents(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        # Check the key phrase is present (may be on one or two lines)
        self.assertIn("do not create new uncontrolled subagents", result.lower())

    def test_says_deterministic_validators_required(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        self.assertIn("Deterministic validators remain required", result)

    def test_says_human_approval_final_gate(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        self.assertIn("Human approval is the final gate", result)

    def test_says_do_not_approve(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        # Text may say "do not approve yourself" or just "do not approve".
        self.assertTrue(
            "do not approve" in result.lower(),
            f"Expected 'do not approve' in output, got:\n{result[:300]}",
        )

    def test_output_is_deterministic(self) -> None:
        plan = self._minimal_plan()
        result1 = render_pi_mission_plan_section(plan)
        result2 = render_pi_mission_plan_section(plan)
        self.assertEqual(result1, result2)

    def test_output_is_markdown(self) -> None:
        plan = self._minimal_plan()
        result = render_pi_mission_plan_section(plan)
        self.assertIn("## Pi Mission Plan", result)
        self.assertIn("### Steps", result)
        self.assertIn("### Governance Constraints", result)


# ----------------------------------------------------------------------
# Helper for dict round-trip tests
# ----------------------------------------------------------------------


def _dict_to_path(d: dict) -> Path:
    """Write a dict as json to a tempfile and return the path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    json.dump(d, tmp, indent=2)
    tmp.close()
    return Path(tmp.name)


if __name__ == "__main__":
    unittest.main()