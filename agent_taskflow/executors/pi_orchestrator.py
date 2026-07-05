"""Pi Mission Orchestrator — build a deterministic multi-step mission plan.

This module is a protocol-level spike. It defines a structured mission plan that
can be rendered into the Pi executor prompt. It does NOT run separate agents,
does NOT call the Pi CLI, does NOT validate, does NOT approve, and does NOT
modify the dispatcher state.

The mission plan is produced by agent-taskflow (not by Pi) and written as
<artifact_dir>/pi_mission_plan.json. It documents the structured roles/steps
that the executor prompt will reference. The plan is governance-safe: every
step inherits forbidden actions, and no step is allowed to approve, push,
merge, or cleanup.

The plan is deterministic: given the same contract, the same steps are
produced in the same order with the same constraints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.mission_contract import (
    MissionContract,
    read_mission_contract,
)

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

SCHEMA_VERSION = "1"
MISSION_CONTRACT_ARTIFACT = "mission_contract.json"
PI_MISSION_PLAN_ARTIFACT = "pi_mission_plan.json"
PI_MISSION_PROMPT_ARTIFACT = "pi_mission_prompt.md"
PI_EXECUTOR_LOG_ARTIFACT = "pi-executor.log"

# Every step must inherit these forbidden actions. They are never removable.
_REQUIRED_FORBIDDEN_ACTIONS = (
    "approve",
    "self_approve",
    "push",
    "force_push",
    "merge",
    "cleanup",
    "delete_worktree",
    "delete_branch",
)


def _dedupe_preserving_order(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)


# ----------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PiMissionStep:
    """One structured step within a Pi mission plan.

    This is protocol metadata only. It is never executed as a separate
    autonomous process. The entire plan is delivered to Pi as part of
    the mission prompt in a single controlled run.
    """

    step_id: str
    role: str
    title: str
    objective: str
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    expected_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("step_id", "role", "title", "objective"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"PiMissionStep.{field_name} must be a non-empty string")
        if not isinstance(self.allowed_actions, tuple):
            object.__setattr__(self, "allowed_actions", tuple(self.allowed_actions))
        if not isinstance(self.forbidden_actions, tuple):
            object.__setattr__(self, "forbidden_actions", tuple(self.forbidden_actions))
        if not isinstance(self.expected_outputs, tuple):
            object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))


@dataclass(frozen=True)
class PiMissionPlan:
    """Deterministic, governance-safe Pi mission plan.

    Produced by build_pi_mission_plan() from a MissionContract. Written to
    <artifact_dir>/pi_mission_plan.json and rendered into pi_mission_prompt.md.
    """

    schema_version: str
    task_key: str
    executor: str
    steps: tuple[PiMissionStep, ...]
    required_validators: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    mission_contract: dict[str, str]
    artifacts: dict[str, str]
    human_approval_required: bool

    def __post_init__(self) -> None:
        if not self.schema_version:
            raise ValueError("schema_version must be non-empty")
        if not self.task_key:
            raise ValueError("task_key must be non-empty")
        if not self.executor:
            raise ValueError("executor must be non-empty")
        if not isinstance(self.steps, tuple):
            object.__setattr__(self, "steps", tuple(self.steps))
        if not isinstance(self.required_validators, tuple):
            object.__setattr__(self, "required_validators", tuple(self.required_validators))
        if not isinstance(self.forbidden_actions, tuple):
            object.__setattr__(self, "forbidden_actions", tuple(self.forbidden_actions))
        if not isinstance(self.mission_contract, dict):
            object.__setattr__(self, "mission_contract", dict(self.mission_contract))
        if not isinstance(self.artifacts, dict):
            object.__setattr__(self, "artifacts", dict(self.artifacts))
        # Safety: verify no step allows dangerous actions
        for step in self.steps:
            overlap = set(step.forbidden_actions) & set(_REQUIRED_FORBIDDEN_ACTIONS)
            if overlap != set(_REQUIRED_FORBIDDEN_ACTIONS):
                missing = set(_REQUIRED_FORBIDDEN_ACTIONS) - set(step.forbidden_actions)
                raise ValueError(
                    f"step {step.step_id!r} is missing required forbidden actions: {missing}"
                )


# ----------------------------------------------------------------------
# Step definitions
# ----------------------------------------------------------------------


def _scout_step(forbidden_actions: tuple[str, ...]) -> PiMissionStep:
    """Scout step: inspect task context and repo constraints."""
    extra_forbidden = tuple(a for a in forbidden_actions if a not in _REQUIRED_FORBIDDEN_ACTIONS)
    return PiMissionStep(
        step_id="scout",
        role="scout",
        title="Inspect task context and repository",
        objective="Inspect the task context, repository structure, and constraints. "
        "Identify any known blockers, code conventions, or dependencies. "
        "Summarize findings for the next step.",
        allowed_actions=(
            "read files",
            "inspect code",
            "list directory contents",
            "run read-only git commands",
            "summarize constraints",
        ),
        forbidden_actions=_REQUIRED_FORBIDDEN_ACTIONS + extra_forbidden,
        expected_outputs=("scout_notes",),
    )


def _planner_step(forbidden_actions: tuple[str, ...]) -> PiMissionStep:
    """Planner step: propose implementation approach."""
    extra_forbidden = tuple(a for a in forbidden_actions if a not in _REQUIRED_FORBIDDEN_ACTIONS)
    return PiMissionStep(
        step_id="planner",
        role="planner",
        title="Propose implementation approach",
        objective="Based on the task goal and scout findings, propose a clear "
        "implementation approach. Identify the files to modify, the changes "
        "to make, and the order in which to proceed. Document risks or "
        "dependencies. Do not start writing implementation code here.",
        allowed_actions=(
            "write plan notes",
            "propose file changes",
            "identify dependencies",
            "list risk areas",
        ),
        forbidden_actions=_REQUIRED_FORBIDDEN_ACTIONS + extra_forbidden,
        expected_outputs=("implementation_plan",),
    )


def _implementer_step(forbidden_actions: tuple[str, ...]) -> PiMissionStep:
    """Implementer step: make code changes inside assigned worktree."""
    extra_forbidden = tuple(a for a in forbidden_actions if a not in _REQUIRED_FORBIDDEN_ACTIONS)
    return PiMissionStep(
        step_id="implementer",
        role="implementer",
        title="Implement code changes",
        objective="Make the actual code changes inside the assigned worktree. "
        "Follow the plan. Write clean, focused diffs. Do not modify files "
        "outside the worktree. Do not commit or push. Leave testing to the "
        "deterministic validators.",
        allowed_actions=(
            "edit files inside worktree",
            "create new files inside worktree",
            "delete files inside worktree",
            "run read-only validation commands",
            "write implementation notes",
        ),
        forbidden_actions=("push", "force_push", "merge", "cleanup", "approve", "self_approve",
                           "delete_worktree", "delete_branch") + extra_forbidden,
        expected_outputs=("code_changes", "implementation_notes"),
    )


def _reviewer_step(forbidden_actions: tuple[str, ...]) -> PiMissionStep:
    """Reviewer step: self-review implementation for obvious issues."""
    extra_forbidden = tuple(a for a in forbidden_actions if a not in _REQUIRED_FORBIDDEN_ACTIONS)
    return PiMissionStep(
        step_id="reviewer",
        role="reviewer",
        title="Self-review implementation",
        objective="Review the implementation for obvious issues: syntax errors, "
        "logic bugs, missing imports, incomplete changes, or deviation from "
        "the task goal. Write a self-review summary. This review does NOT "
        "replace deterministic validators.",
        allowed_actions=(
            "inspect diff",
            "read modified files",
            "run read-only lint/typecheck commands",
            "write self_review_notes",
        ),
        forbidden_actions=(
            "approve", "self_approve", "push", "force_push", "merge", "cleanup",
            "delete_worktree", "delete_branch", "replace validators", "mark complete"
        ) + extra_forbidden,
        expected_outputs=("self_review_notes",),
    )


def _handoff_step(forbidden_actions: tuple[str, ...]) -> PiMissionStep:
    """Handoff step: summarize worker output for review."""
    extra_forbidden = tuple(a for a in forbidden_actions if a not in _REQUIRED_FORBIDDEN_ACTIONS)
    return PiMissionStep(
        step_id="handoff",
        role="handoff",
        title="Summarize output for review",
        objective="Summarize the work done, the files changed, any risks "
        "identified, and notes for the human reviewer and deterministic "
        "validators. Write a clear handoff summary. Do not approve, push, "
        "merge, or cleanup.",
        allowed_actions=(
            "write handoff notes",
            "list changed files",
            "summarize findings",
        ),
        forbidden_actions=_REQUIRED_FORBIDDEN_ACTIONS + extra_forbidden,
        expected_outputs=("handoff_summary",),
    )


# ----------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------


def build_pi_mission_plan(
    contract: MissionContract | dict,
) -> PiMissionPlan:
    """Build a deterministic Pi mission plan from a MissionContract or dict.

    The plan is always the same 5 steps (scout, planner, implementer, reviewer,
    handoff) with the same governance constraints. The contract provides the
    forbidden_actions and required_validators that are embedded in each step.

    Parameters
    ----------
    contract
        Either a MissionContract dataclass or a plain dict parsed from
        mission_contract.json.

    Returns
    -------
    PiMissionPlan
        A frozen, deterministic mission plan.

    Raises
    ------
    TypeError
        If contract is not a MissionContract or dict.
    ValueError
        If required contract fields are missing.
    """
    if not isinstance(contract, (MissionContract, dict)):
        raise TypeError(
            f"contract must be a MissionContract or dict, "
            f"not {type(contract).__name__!r}"
        )

    if isinstance(contract, MissionContract):
        task_key = contract.task_key
        executor = contract.executor
        contract_forbidden = contract.forbidden_actions
        required_validators = contract.required_validators
        human_approval = contract.human_approval_required
        contract_schema_version = contract.schema_version
    else:
        task_key = contract.get("task_key", "")
        executor = contract.get("executor", "")
        contract_forbidden = tuple(contract.get("forbidden_actions", []) or [])
        required_validators = tuple(contract.get("required_validators", []) or [])
        human_approval = bool(contract.get("human_approval_required", True))
        contract_schema_version = str(contract.get("schema_version", SCHEMA_VERSION))

    if not task_key or not str(task_key).strip():
        raise ValueError("contract is missing required field: 'task_key'")
    if not executor or not str(executor).strip():
        raise ValueError("contract is missing required field: 'executor'")

    forbidden = _dedupe_preserving_order(_REQUIRED_FORBIDDEN_ACTIONS + contract_forbidden)

    plan = PiMissionPlan(
        schema_version=SCHEMA_VERSION,
        task_key=task_key,
        executor=executor,
        steps=(
            _scout_step(forbidden),
            _planner_step(forbidden),
            _implementer_step(forbidden),
            _reviewer_step(forbidden),
            _handoff_step(forbidden),
        ),
        required_validators=required_validators,
        forbidden_actions=forbidden,
        mission_contract={
            "artifact_name": MISSION_CONTRACT_ARTIFACT,
            "schema_version": contract_schema_version,
        },
        artifacts={
            "mission_contract": MISSION_CONTRACT_ARTIFACT,
            "mission_plan": PI_MISSION_PLAN_ARTIFACT,
            "mission_prompt": PI_MISSION_PROMPT_ARTIFACT,
            "executor_log": PI_EXECUTOR_LOG_ARTIFACT,
        },
        human_approval_required=human_approval,
    )
    return plan


# ----------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------


def pi_mission_plan_to_dict(plan: PiMissionPlan) -> dict:
    """Convert a PiMissionPlan to a JSON-safe dict."""
    return {
        "schema_version": plan.schema_version,
        "task_key": plan.task_key,
        "executor": plan.executor,
        "steps": [
            {
                "step_id": step.step_id,
                "role": step.role,
                "title": step.title,
                "objective": step.objective,
                "allowed_actions": list(step.allowed_actions),
                "forbidden_actions": list(step.forbidden_actions),
                "expected_outputs": list(step.expected_outputs),
            }
            for step in plan.steps
        ],
        "required_validators": list(plan.required_validators),
        "forbidden_actions": list(plan.forbidden_actions),
        "mission_contract": dict(plan.mission_contract),
        "artifacts": dict(plan.artifacts),
        "human_approval_required": plan.human_approval_required,
    }


def read_pi_mission_plan(path: str | Path) -> PiMissionPlan:
    """Read and deserialize a pi_mission_plan.json file.

    Returns a PiMissionPlan.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the JSON is invalid or schema fields are missing.
    TypeError
        If field types are wrong.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"pi_mission_plan.json not found: {p}")

    try:
        raw = p.read_text(encoding="utf-8")
        d = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pi_mission_plan.json is not valid JSON: {exc}") from exc

    if not isinstance(d, dict):
        raise ValueError("pi_mission_plan.json must be a JSON object")

    # Basic validation
    for field_name in ("schema_version", "task_key", "executor", "steps"):
        if field_name not in d:
            raise ValueError(f"pi_mission_plan.json missing required field: {field_name!r}")

    # Reconstruct PiMissionPlan from dict (bypass __post_init__ safety for raw input)
    steps: list[PiMissionStep] = []
    for step_dict in d.get("steps", []):
        if not isinstance(step_dict, dict):
            raise TypeError("pi_mission_plan.json steps must be objects")
        steps.append(
            PiMissionStep(
                step_id=str(step_dict.get("step_id", "")),
                role=str(step_dict.get("role", "")),
                title=str(step_dict.get("title", "")),
                objective=str(step_dict.get("objective", "")),
                allowed_actions=tuple(step_dict.get("allowed_actions", []) or []),
                forbidden_actions=tuple(step_dict.get("forbidden_actions", []) or []),
                expected_outputs=tuple(step_dict.get("expected_outputs", []) or []),
            )
        )

    return PiMissionPlan(
        schema_version=str(d["schema_version"]),
        task_key=str(d["task_key"]),
        executor=str(d["executor"]),
        steps=tuple(steps),
        required_validators=tuple(d.get("required_validators", []) or []),
        forbidden_actions=tuple(d.get("forbidden_actions", []) or []),
        mission_contract=dict(
            d.get("mission_contract")
            or {
                "artifact_name": MISSION_CONTRACT_ARTIFACT,
                "schema_version": SCHEMA_VERSION,
            }
        ),
        artifacts=dict(
            d.get("artifacts")
            or {
                "mission_contract": MISSION_CONTRACT_ARTIFACT,
                "mission_plan": PI_MISSION_PLAN_ARTIFACT,
                "mission_prompt": PI_MISSION_PROMPT_ARTIFACT,
                "executor_log": PI_EXECUTOR_LOG_ARTIFACT,
            }
        ),
        human_approval_required=bool(d.get("human_approval_required", True)),
    )


# ----------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------


def write_pi_mission_plan(
    artifact_dir: Path,
    plan: PiMissionPlan,
) -> Path:
    """Write the Pi mission plan to the artifact directory.

    The output file is always ``<artifact_dir>/pi_mission_plan.json``.
    The artifact directory is created if it does not exist.

    Parameters
    ----------
    artifact_dir
        The task artifact directory. Must resolve to an absolute path.
    plan
        The PiMissionPlan to serialize and write.

    Returns
    -------
    Path
        The path to the written file.

    Raises
    ------
    ValueError
        If artifact_dir is not absolute or if the output path would escape.
    """
    if not isinstance(artifact_dir, Path):
        artifact_dir = Path(artifact_dir)

    resolved_dir = artifact_dir.resolve()
    if not resolved_dir.is_absolute():
        raise ValueError("artifact_dir must be an absolute path")

    output_path = resolved_dir / "pi_mission_plan.json"

    # Defensive path traversal check.
    try:
        output_path.relative_to(resolved_dir)
    except ValueError as exc:
        raise ValueError(
            "output path would escape artifact directory — possible traversal attempt"
        ) from exc

    resolved_dir.mkdir(parents=True, exist_ok=True)
    d = pi_mission_plan_to_dict(plan)
    atomic_write_json(output_path, d, sort_keys=True, trailing_newline=False)
    return output_path


# ----------------------------------------------------------------------
# Renderer
# ----------------------------------------------------------------------


def render_pi_mission_plan_section(
    plan: PiMissionPlan,
) -> str:
    """Render the mission plan as a markdown section for the Pi prompt.

    This produces a deterministic, governance-safe section that:
    - Lists all steps with their roles, objectives, and constraints.
    - Explicitly states these are protocol steps, not autonomous agents.
    - Emphasizes deterministic validators remain required.
    - Emphasizes human approval is the final gate.
    - States do not create uncontrolled subagents.

    Parameters
    ----------
    plan
        The PiMissionPlan to render.

    Returns
    -------
    str
        A markdown string suitable for embedding in the Pi mission prompt.
    """
    lines: list[str] = []

    lines.append("## Pi Mission Plan\n")
    lines.append(
        "**These are structured protocol steps, not independent autonomous agents.**\n"
        "Complete them within one controlled Pi executor run.\n"
        "**Do not create new uncontrolled subagents.**\n"
        "Do not bypass deterministic validators.\n"
        "Do not mark the task approved or complete.\n"
    )
    lines.append("### Steps\n")
    for i, step in enumerate(plan.steps, start=1):
        lines.append(f"#### Step {i}: {step.title}\n")
        lines.append(f"- **Role:** `{step.role}`\n")
        lines.append(f"- step_id: `{step.step_id}`\n")
        lines.append(f"- **Objective:** {step.objective}\n")
        lines.append("- **Allowed actions:**\n")
        if step.allowed_actions:
            for action in step.allowed_actions:
                lines.append(f"  - `{action}`\n")
        else:
            lines.append("  - *(none — read-only)*\n")
        lines.append("- **Forbidden actions:**\n")
        for action in step.forbidden_actions:
            lines.append(f"  - `{action}` — strictly prohibited\n")
        lines.append("- **Expected outputs:**\n")
        for output in step.expected_outputs:
            lines.append(f"  - `{output}`\n")
        lines.append("\n")

    lines.append("### Governance Constraints\n")
    lines.append(
        "- **Deterministic validators remain required.** AI reviewers and mission\n"
        "  loops cannot replace deterministic validators (pytest, openspec, policy,\n"
        "  typecheck, lint).\n"
        "- **Human approval is the final gate.** Only the designated human approver\n"
        "  can approve. Do not approve yourself or mark the task complete.\n"
        "- **No ungoverned subagents.** Do not spawn additional agents outside the\n"
        "  defined steps above.\n"
        "- **Worktree boundary.** Work only inside the assigned worktree path.\n"
        "  Do not modify the main repository directly.\n"
    )

    return "".join(lines)


__all__ = [
    "PiMissionPlan",
    "PiMissionStep",
    "build_pi_mission_plan",
    "pi_mission_plan_to_dict",
    "read_pi_mission_plan",
    "render_pi_mission_plan_section",
    "write_pi_mission_plan",
]
