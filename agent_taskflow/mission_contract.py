"""Mission Contract artifact for Agent Taskflow.

A Mission Contract is a JSON artifact produced at dispatch time. It documents
the task intent, executor configuration, required validators, and the explicit
governance rules that apply to every executor run.

The contract is:
- Produced before the executor runs (write_mission_contract is called by the
  dispatcher before the executor is invoked).
- Written to <artifact_dir>/mission_contract.json so it is readable by humans
  and by future executor adapters.
- Immutable once written (the dispatcher always writes a fresh contract per run).
- Free of secret-like values (API keys, tokens, passwords, credentials).

The contract is NOT:
- A validator (it does not block on its own).
- An orchestrator (it does not drive loops or sub-agents).
- A replacement for the governance layer (agent-taskflow remains the control plane).
- A replacement for deterministic validators (pytest, openspec, typecheck, lint,
  policy checks remain required regardless of what the executor produces).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_taskflow.models import require_absolute_path


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

SCHEMA_VERSION = "1"

# Hardcoded governance rules that every contract must include.
# These can never be overridden by an executor or task configuration.
_GOVERNANCE_FORBIDDEN_ACTIONS = (
    "approve",
    "push",
    "merge",
    "cleanup",
    "delete_worktree",
    "delete_branch",
    "self_approve",
    "force_push",
)

_GOVERNANCE_EXPECTED_ARTIFACTS = (
    "executor_log",
    "validator_logs",
    "git_status",
    "git_diff",
)

# Env var names that indicate a secret; their values must not appear in a
# contract (they may be passed at runtime via env= context.env, but that
# bypasses the contract write path).
_SECRET_ENV_MARKERS = frozenset(
    marker.lower() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
)

# Fields that, if present in a dict, indicate a secret value and must cause
# a ValueError / TypeError at contract build time.
_SECRET_FIELD_PATTERNS = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "access_token",
    "refresh_token",
    "authorization",
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(pat in normalized for pat in _SECRET_FIELD_PATTERNS)


def _dict_has_secret_values(d: dict) -> bool:
    """Return True if d contains any secret-like key or value."""
    for key in d:
        if _is_secret_key(key):
            return True
    for value in d.values():
        if isinstance(value, str) and any(
            marker in value.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            return True
        if isinstance(value, dict) and _dict_has_secret_values(value):
            return True
    return False


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


# ----------------------------------------------------------------------
# Dataclass
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MissionContract:
    """Frozen artifact that documents task intent and governance rules.

    Produced by build_mission_contract(...) at dispatch time and written to
    <artifact_dir>/mission_contract.json.  Consumers (human reviewers,
    future executor adapters, tooling) can read the contract to understand
    what was approved and what constraints apply.
    """

    schema_version: str
    task_key: str
    goal: str
    repo_path: Path
    worktree_path: Path
    artifact_dir: Path
    executor: str
    required_validators: tuple[str, ...] = field(default_factory=lambda: ("pytest", "openspec"))
    forbidden_actions: tuple[str, ...] = field(default_factory=lambda: _GOVERNANCE_FORBIDDEN_ACTIONS)
    expected_artifacts: tuple[str, ...] = field(default_factory=lambda: _GOVERNANCE_EXPECTED_ARTIFACTS)
    human_approval_required: bool = field(default_factory=lambda: True)
    # Optional fields
    title: str | None = None
    model: str | None = None
    provider: str | None = None
    implementation_prompt_path: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Always convert strings to absolute Path objects.
        object.__setattr__(
            self,
            "schema_version",
            _require_non_empty(self.schema_version, "schema_version"),
        )
        object.__setattr__(
            self,
            "task_key",
            _require_non_empty(self.task_key, "task_key"),
        )
        object.__setattr__(
            self,
            "goal",
            _require_non_empty(self.goal, "goal"),
        )
        object.__setattr__(
            self,
            "executor",
            _require_non_empty(self.executor, "executor"),
        )
        # Convert string paths to absolute Path objects.
        object.__setattr__(
            self,
            "repo_path",
            require_absolute_path(self.repo_path, "repo_path"),
        )
        object.__setattr__(
            self,
            "worktree_path",
            require_absolute_path(self.worktree_path, "worktree_path"),
        )
        object.__setattr__(
            self,
            "artifact_dir",
            require_absolute_path(self.artifact_dir, "artifact_dir"),
        )

    @property
    def governance_rules(self) -> list[str]:
        """Human-readable list of governance constraints embedded in the contract."""
        return [
            "agent-taskflow is the governance/control plane.",
            "Pi, OpenCode, and Shell are executor backends only.",
            "Worker cannot approve tasks.",
            "Worker cannot push to remote branches.",
            "Worker cannot merge PRs.",
            "Worker cannot cleanup worktrees.",
            "Worker cannot delete branches.",
            "Worker cannot self-approve.",
            "Worker cannot force-push.",
            "AI reviewer/auditor cannot replace deterministic validators.",
            "Deterministic validators remain required regardless of executor output.",
            "Human approval is the final gate.",
            "Artifacts/logs/validation results must be traceable and rerunnable.",
        ]


# ----------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------


def build_mission_contract(
    *,
    task_key: str,
    goal: str,
    repo_path: str | Path,
    worktree_path: str | Path,
    artifact_dir: str | Path,
    executor: str,
    title: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    required_validators: tuple[str, ...] | None = None,
    implementation_prompt_path: Path | str | None = None,
    extra: dict[str, str] | None = None,
) -> MissionContract:
    """Build a MissionContract from raw values.

    Raises
    ------
    ValueError
        If required fields are missing or empty.
    TypeError
        If extra contains secret-like keys.
    """
    repo_path = require_absolute_path(repo_path, "repo_path")
    worktree_path = require_absolute_path(worktree_path, "worktree_path")
    artifact_dir = require_absolute_path(artifact_dir, "artifact_dir")

    if extra is not None and _dict_has_secret_values(extra):
        raise TypeError(
            "extra dict must not contain secret-like keys (key, token, secret, "
            "password, credential, api_key, access_token, authorization)"
        )

    resolved_prompt_path: Path | None = None
    if implementation_prompt_path is not None:
        resolved_prompt_path = require_absolute_path(
            implementation_prompt_path, "implementation_prompt_path"
        )

    return MissionContract(
        schema_version=SCHEMA_VERSION,
        task_key=_require_non_empty(task_key, "task_key"),
        title=title,
        goal=_require_non_empty(goal, "goal"),
        repo_path=repo_path,
        worktree_path=worktree_path,
        artifact_dir=artifact_dir,
        executor=_require_non_empty(executor, "executor"),
        model=model,
        provider=provider,
        required_validators=required_validators or ("pytest", "openspec"),
        implementation_prompt_path=resolved_prompt_path,
        extra=extra or {},
    )


# ----------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------


def mission_contract_to_dict(contract: MissionContract) -> dict:
    """Convert a MissionContract to a JSON-safe dict.

    All Path values become strings. Secret-like keys are never present
    (enforced by build_mission_contract).
    """
    d: dict = {
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
    }
    if contract.model is not None:
        d["model"] = contract.model
    if contract.provider is not None:
        d["provider"] = contract.provider
    if contract.title is not None:
        d["title"] = contract.title
    if contract.implementation_prompt_path is not None:
        d["implementation_prompt_path"] = str(contract.implementation_prompt_path)
    if contract.extra:
        d["extra"] = contract.extra
    return d


def _validate_dict(d: dict) -> None:
    """Validate a dict against the expected Mission Contract schema."""
    required_strings = (
        "schema_version",
        "task_key",
        "goal",
        "repo_path",
        "worktree_path",
        "artifact_dir",
        "executor",
    )
    for field_name in required_strings:
        if field_name not in d:
            raise ValueError(f"mission_contract.json missing required field: {field_name}")
        if not isinstance(d[field_name], str):
            raise TypeError(f"mission_contract.json field {field_name!r} must be a string")
        if not d[field_name].strip():
            raise ValueError(f"mission_contract.json field {field_name!r} must not be empty")

    if not isinstance(d.get("required_validators", []), list):
        raise TypeError("mission_contract.json required_validators must be a list")
    if not isinstance(d.get("forbidden_actions", []), list):
        raise TypeError("mission_contract.json forbidden_actions must be a list")
    if not isinstance(d.get("human_approval_required", False), bool):
        raise TypeError("mission_contract.json human_approval_required must be a bool")


def read_mission_contract(path: str | Path) -> dict:
    """Read and validate a mission_contract.json file.

    Returns a JSON-safe dict (paths are strings, not Path objects).

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If required fields are missing or empty.
    TypeError
        If field types are wrong.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"mission_contract.json not found: {p}")

    try:
        raw = p.read_text(encoding="utf-8")
        d = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"mission_contract.json is not valid JSON: {exc}") from exc

    if not isinstance(d, dict):
        raise ValueError("mission_contract.json must be a JSON object")

    _validate_dict(d)
    return d


# ----------------------------------------------------------------------
# Write
# ----------------------------------------------------------------------


def write_mission_contract(
    contract: MissionContract,
    *,
    artifact_dir: str | Path | None = None,
    path: str | Path | None = None,
) -> Path:
    """Write a MissionContract to <artifact_dir>/mission_contract.json.

    Exactly one of artifact_dir or path must be provided.

    Parameters
    ----------
    contract
        The MissionContract to serialize and write.
    artifact_dir
        Artifact directory for the task.  Path is derived as
        <artifact_dir>/mission_contract.json.
    path
        Explicit path to the output file.

    Returns
    -------
    Path
        The path the file was written to.

    Raises
    ------
    ValueError
        If neither or both of artifact_dir and path are provided.
    FileNotFoundError
        If the parent directory does not exist (artifact_dir.mkdir is NOT
        created by this function — the dispatcher or caller must ensure the
        artifact directory exists before calling this helper).
    """
    if (artifact_dir is None) == (path is None):
        raise ValueError("Provide exactly one of artifact_dir or path")

    if artifact_dir is not None:
        artifact_dir = Path(artifact_dir).expanduser().resolve()
        output_path = artifact_dir / "mission_contract.json"
    else:
        assert path is not None
        output_path = Path(path).expanduser().resolve()

    d = mission_contract_to_dict(contract)
    raw = json.dumps(d, indent=2, sort_keys=True)
    output_path.write_text(raw, encoding="utf-8")
    return output_path


# ----------------------------------------------------------------------
# Convenience: build from TaskRecord (requires no store/DB dependency)
# ----------------------------------------------------------------------


def build_from_task_fields(
    *,
    task_key: str,
    goal: str,
    repo_path: str | Path,
    worktree_path: str | Path,
    artifact_dir: str | Path,
    executor: str,
    model: str | None = None,
    provider: str | None = None,
    required_validators: tuple[str, ...] | None = None,
    implementation_prompt_path: Path | str | None = None,
) -> MissionContract:
    """Convenience wrapper for build_mission_contract without extra.

    This is the recommended entry point for the dispatcher, which will
    typically not pass extra fields.
    """
    return build_mission_contract(
        task_key=task_key,
        goal=goal,
        repo_path=repo_path,
        worktree_path=worktree_path,
        artifact_dir=artifact_dir,
        executor=executor,
        model=model,
        provider=provider,
        required_validators=required_validators,
        implementation_prompt_path=implementation_prompt_path,
        extra=None,
    )


__all__ = [
    "MissionContract",
    "build_from_task_fields",
    "build_mission_contract",
    "mission_contract_to_dict",
    "read_mission_contract",
    "write_mission_contract",
]