"""Pi Mission Protocol — render a Mission Contract into a Pi-friendly prompt.

This module is intentionally read-only and deterministic. It never calls the
dispatcher, never modifies task state, never approves, and never calls any
external service.

It produces a self-contained markdown prompt that a Pi executor backend can use
as its primary input. The prompt encodes governance boundaries, required
validators, forbidden actions, and execution constraints so that Pi operates
within the agent-taskflow control plane.

The output file (pi_mission_prompt.md) is always written inside the task
artifact directory. It is never written outside it.
"""

from __future__ import annotations

from pathlib import Path

from agent_taskflow.mission_contract import (
    MissionContract,
    read_mission_contract,
)

# High-confidence secret patterns — same as PolicyCheckValidator.
_SECRET_PATTERNS = (
    # env-style: KEY=value, KEY:value, KEY = value
    __import__("re").compile(
        r"[A-Z_][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*[:=]",
        __import__("re").IGNORECASE,
    ),
    __import__("re").compile(
        r'"[A-Za-z_]*(?:api_key|token|secret|password|credential|access_token|refresh_token|authorization)"\s*:\s*"[^"]+',
        __import__("re").IGNORECASE,
    ),
    __import__("re").compile(
        r"(?:api_key|token|secret)\s*=\s*[\"']?(?:sk-|ak-)[A-Za-z0-9_-]{10,}",
        __import__("re").IGNORECASE,
    ),
    __import__("re").compile(
        r"[A-Z_][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*=\s*\S+",
        __import__("re").IGNORECASE,
    ),
)


def _has_secrets(text: str) -> bool:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _contract_to_dict(contract: MissionContract | dict) -> dict:
    """Convert a MissionContract or already-parsed dict to a plain dict."""
    if isinstance(contract, MissionContract):
        return {
            "schema_version": contract.schema_version,
            "task_key": contract.task_key,
            "goal": contract.goal,
            "repo_path": str(contract.repo_path),
            "worktree_path": str(contract.worktree_path),
            "artifact_dir": str(contract.artifact_dir),
            "executor": contract.executor,
            "required_validators": list(contract.required_validators),
            "forbidden_actions": list(contract.forbidden_actions),
            "expected_artifacts": list(contract.expected_artifacts),
            "human_approval_required": contract.human_approval_required,
            "governance_rules": contract.governance_rules,
            "model": getattr(contract, "model", None),
            "provider": getattr(contract, "provider", None),
            "title": getattr(contract, "title", None),
        }
    return dict(contract)


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------


def render_pi_mission_prompt(
    contract: MissionContract | dict,
    *,
    original_prompt: str | None = None,
) -> str:
    """Render a Mission Contract as a Pi-friendly markdown mission prompt.

    Parameters
    ----------
    contract
        Either a MissionContract dataclass or a plain dict parsed from
        mission_contract.json.
    original_prompt
        Optional raw task prompt text to append at the end of the prompt.
        If the text contains high-confidence secret patterns it is omitted
        and replaced with a placeholder.

    Returns
    -------
    str
        A self-contained markdown prompt string.

    Raises
    ------
    TypeError
        If contract is not a MissionContract or dict.
    ValueError
        If required fields are missing from the contract.
    """
    if not isinstance(contract, (MissionContract, dict)):
        raise TypeError(
            f"contract must be a MissionContract or dict, "
            f"not {type(contract).__name__!r}"
        )

    d = _contract_to_dict(contract)

    # Validate required fields.
    for field_name in ("task_key", "goal", "executor", "repo_path",
                       "worktree_path", "artifact_dir"):
        value = d.get(field_name)
        if not value or not str(value).strip():
            raise ValueError(f"contract is missing required field: {field_name!r}")

    lines: list[str] = []

    # Header
    lines.append("# Pi Mission Protocol\n")
    lines.append(
        "**IMPORTANT: Read this document carefully before taking any action. "
        "This document defines your mission scope, constraints, and governance "
        "rules. Violating any governance rule may result in task rejection.**\n"
    )

    # Mission Goal
    lines.append("## Mission Goal\n")
    lines.append(f"{d['goal']}\n")
    if d.get("title"):
        lines.append(f"**Title:** {d['title']}\n")
    lines.append(f"**Task key:** {d['task_key']}\n")

    # Working Context
    lines.append("\n## Working Context\n")
    lines.append(f"- **Repository path:** `{d['repo_path']}`\n")
    lines.append(f"- **Worktree path:** `{d['worktree_path']}`\n")
    lines.append(f"- **Artifact directory:** `{d['artifact_dir']}`\n")
    if d.get("model"):
        lines.append(f"- **Model:** `{d['model']}`\n")
    if d.get("provider"):
        lines.append(f"- **Provider:** `{d['provider']}`\n")

    # Required Validators
    validators = d.get("required_validators") or []
    lines.append("\n## Required Deterministic Validators\n")
    lines.append(
        "**You are responsible for producing code changes only.** "
        "The following deterministic validators will run automatically after "
        "your work is complete. You must not skip, bypass, or replace them:\n"
    )
    if validators:
        for v in validators:
            lines.append(f"- `{v}`\n")
    else:
        lines.append("- *(none specified — check with the task author)*\n")
    lines.append(
        "\n> **AI reviewers, mission loops, and self-assessment do not replace "
        "deterministic validators.**\n"
    )

    # Forbidden Actions
    forbidden = d.get("forbidden_actions") or []
    lines.append("\n## Forbidden Actions\n")
    lines.append(
        "**You must NEVER perform any of the following actions, even if asked "
        "or implied by the task goal:**\n"
    )
    if forbidden:
        for action in forbidden:
            lines.append(f"- **{action}** — strictly prohibited\n")
    else:
        lines.append("- *(none listed — standard governance rules apply)*\n")
    lines.append(
        "\n> **Special prohibitions:**\n"
        "> - Do NOT approve tasks. Only the designated human approver can approve.\n"
        "> - Do NOT push to remote branches.\n"
        "> - Do NOT merge pull requests.\n"
        "> - Do NOT run cleanup operations.\n"
        "> - Do NOT delete worktrees or branches.\n"
        "> - Do NOT force-push.\n"
        "> - Do NOT modify the main repository directly; only work in the assigned worktree.\n"
    )

    # Expected Artifacts
    expected = d.get("expected_artifacts") or []
    lines.append("\n## Expected Artifacts\n")
    lines.append(
        "Your work must produce or update the following artifacts "
        "(exact names and paths may vary):\n"
    )
    if expected:
        for a in expected:
            lines.append(f"- `{a}`\n")
    else:
        lines.append("- *(none specified)*\n")

    # Governance Rules
    rules = d.get("governance_rules") or []
    lines.append("\n## Governance Rules\n")
    lines.append(
        "**agent-taskflow is the governance and control plane.** "
        "You are an executor backend only.\n"
    )
    # Emit embedded rules from the contract if present.
    for rule in rules:
        lines.append(f"- {rule}\n")
    # Always append hard rules that must never be omitted.
    hard_rules = [
        "Human approval is the final gate before any merge or deployment.",
        "Deterministic validators (pytest, openspec, policy, typecheck, lint) "
        "are mandatory regardless of executor output.",
        "AI reviewers and mission loops cannot replace deterministic validators.",
        "If you detect a conflict between task instructions and governance rules, "
        "governance rules take precedence.",
    ]
    for rule in hard_rules:
        if rule not in rules:
            lines.append(f"- {rule}\n")

    # Execution Instructions
    lines.append("\n## Execution Instructions\n")
    lines.append(
        "1. Work only inside the assigned worktree path.\n"
        "2. Do not modify the main repository branch directly.\n"
        "3. Produce implementation changes as diffs, patches, or commits in the worktree.\n"
        "4. Leave all validation to agent-taskflow's deterministic validators.\n"
        "5. Do not run git push, git merge, or any destructive operation.\n"
        "6. If you need to store notes or summaries, write them as artifact "
        "files in the artifact directory.\n"
        "7. After completing your work, stop — do not wait for approval or "
        "attempt to approve yourself.\n"
    )

    # Original Prompt
    if original_prompt is not None:
        lines.append("\n## Original Task Prompt\n")
        if _has_secrets(original_prompt):
            lines.append(
                "*(original prompt omitted — contains high-confidence secret-like "
                "assignment; review the artifact directory directly)*\n"
            )
        else:
            lines.append(f"{original_prompt.strip()}\n")

    lines.append(
        "\n---\n"
        "*This prompt was generated by agent-taskflow. "
        "It is governed by the Mission Contract at the root of the artifact directory. "
        "Do not modify or delete this file.*\n"
    )

    return "".join(lines)


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------


def write_pi_mission_prompt(
    artifact_dir: Path,
    content: str,
) -> Path:
    """Write the rendered Pi mission prompt to the artifact directory.

    The output file is always ``<artifact_dir>/pi_mission_prompt.md``.
    The artifact directory is created if it does not exist.

    Parameters
    ----------
    artifact_dir
        The task artifact directory. Must resolve to an absolute path.
    content
        The rendered prompt text.

    Returns
    -------
    Path
        The path to the written file.

    Raises
    ------
    ValueError
        If artifact_dir is not absolute or if the resolved output path
        would escape artifact_dir (path traversal attempt).
    """
    if not isinstance(artifact_dir, Path):
        artifact_dir = Path(artifact_dir)

    resolved_dir = artifact_dir.resolve()
    if not resolved_dir.is_absolute():
        raise ValueError("artifact_dir must be an absolute path")

    output_path = resolved_dir / "pi_mission_prompt.md"

    # Defensive: ensure output_path is inside resolved_dir.
    try:
        output_path.relative_to(resolved_dir)
    except ValueError as exc:
        raise ValueError(
            "output path would escape artifact directory — possible traversal attempt"
        ) from exc

    resolved_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------


def load_contract_for_pi(artifact_dir: Path) -> MissionContract | dict | None:
    """Load and return the mission contract dict from artifact_dir, or None.

    Returns None if the file does not exist or is not valid JSON.
    Raises other exceptions (TypeError, ValueError) for schema violations.
    """
    if not isinstance(artifact_dir, Path):
        artifact_dir = Path(artifact_dir)

    contract_path = artifact_dir / "mission_contract.json"
    if not contract_path.exists():
        return None

    return read_mission_contract(contract_path)


__all__ = [
    "load_contract_for_pi",
    "render_pi_mission_prompt",
    "write_pi_mission_prompt",
]
