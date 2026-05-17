"""Isolated loader for draft machine-readable workflow policies.

This module reads and validates workflow policy JSON data only. It is not wired
into dispatch, executor selection, validator registry behavior, API behavior,
GitHub integration, Mission Control, or workspace management.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "orchestration_boundary",
    "allowed_executors",
    "required_validators",
    "path_policy",
    "workspace_policy",
    "proof_of_work",
    "human_review",
    "forbidden_actions",
    "deferred_integrations",
)

_AI_WORKER_FALSE_FLAGS = (
    "ai_workers_may_schedule_tasks",
    "ai_workers_may_approve",
    "ai_workers_may_merge",
    "ai_workers_may_push",
    "ai_workers_may_cleanup",
)

_REQUIRED_FORBIDDEN_ACTIONS = (
    "push",
    "merge",
    "cleanup",
    "self_approve",
)


def _validate_name_list(value: Any, field_name: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{field_name} must be a non-empty list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field_name}[{index}] must be a non-empty string")


def _is_safe_repo_relative_path(value: str) -> bool:
    normalized = value.strip().replace("\\", "/")
    if not normalized or normalized in {".", ".."}:
        return False
    if Path(normalized).is_absolute() or normalized.startswith("/"):
        return False
    parts = normalized.strip("/").split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _validate_path_list(value: Any, field_name: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{field_name}[{index}] must be a string")
            continue
        if not _is_safe_repo_relative_path(item):
            errors.append(f"{field_name}[{index}] must be a safe repo-relative path")


@dataclass(frozen=True)
class WorkflowPolicyValidationResult:
    """Validation result for a draft workflow policy."""

    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowPolicy:
    """Minimal structured representation of a workflow policy JSON document."""

    schema_version: Any
    orchestration_boundary: Any
    allowed_executors: Any
    required_validators: Any
    optional_validators: Any
    path_policy: Any
    workspace_policy: Any
    proof_of_work: Any
    human_review: Any
    forbidden_actions: Any
    deferred_integrations: Any
    source_path: Path
    raw_data: dict[str, Any]

    def validate(self) -> WorkflowPolicyValidationResult:
        """Validate required fields and governance invariants."""
        errors: list[str] = []
        warnings: list[str] = []

        for key in _REQUIRED_TOP_LEVEL_KEYS:
            if key not in self.raw_data:
                errors.append(f"Missing required workflow policy key: {key}")

        if not self.schema_version:
            errors.append("schema_version must not be empty")

        if not isinstance(self.orchestration_boundary, dict):
            errors.append("orchestration_boundary must be an object")
        else:
            for flag in _AI_WORKER_FALSE_FLAGS:
                if self.orchestration_boundary.get(flag) is not False:
                    errors.append(f"orchestration_boundary.{flag} must be false")

        _validate_name_list(self.allowed_executors, "allowed_executors", errors)

        _validate_name_list(self.required_validators, "required_validators", errors)

        if not isinstance(self.optional_validators, list):
            errors.append("optional_validators must be a list")
        else:
            for index, item in enumerate(self.optional_validators):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"optional_validators[{index}] must be a non-empty string")

        if not isinstance(self.path_policy, dict):
            errors.append("path_policy must be an object")
        else:
            if "allowed_paths" not in self.path_policy:
                errors.append("path_policy.allowed_paths is required")
            else:
                _validate_path_list(
                    self.path_policy.get("allowed_paths"),
                    "path_policy.allowed_paths",
                    errors,
                )
            if "forbidden_paths" not in self.path_policy:
                errors.append("path_policy.forbidden_paths is required")
            else:
                _validate_path_list(
                    self.path_policy.get("forbidden_paths"),
                    "path_policy.forbidden_paths",
                    errors,
                )

        if not isinstance(self.workspace_policy, dict):
            errors.append("workspace_policy must be an object")

        if not isinstance(self.proof_of_work, dict):
            errors.append("proof_of_work must be an object")

        if not isinstance(self.human_review, dict):
            errors.append("human_review must be an object")
        elif self.human_review.get("required") is not True:
            errors.append("human_review.required must be true")

        if not isinstance(self.forbidden_actions, list) or not self.forbidden_actions:
            errors.append("forbidden_actions must be a non-empty list")
        else:
            for index, action in enumerate(self.forbidden_actions):
                if not isinstance(action, str) or not action.strip():
                    errors.append(f"forbidden_actions[{index}] must be a non-empty string")
            forbidden_actions = set(self.forbidden_actions)
            for action in _REQUIRED_FORBIDDEN_ACTIONS:
                if action not in forbidden_actions:
                    errors.append(f"forbidden_actions must include {action}")

        if "deferred_integrations" in self.raw_data and not isinstance(self.deferred_integrations, list):
            errors.append("deferred_integrations must be a list")

        return WorkflowPolicyValidationResult(
            passed=not errors,
            errors=errors,
            warnings=warnings,
        )


def load_workflow_policy(path: Path) -> WorkflowPolicy:
    """Load a workflow policy JSON document without side effects."""
    source_path = Path(path)
    try:
        raw_text = source_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"workflow policy file not found: {source_path}") from exc

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid workflow policy JSON in {source_path}: {exc.msg}") from exc

    if not isinstance(raw_data, dict):
        raise ValueError(f"workflow policy JSON must be an object: {source_path}")

    preserved_raw_data = copy.deepcopy(raw_data)
    return WorkflowPolicy(
        schema_version=preserved_raw_data.get("schema_version"),
        orchestration_boundary=preserved_raw_data.get("orchestration_boundary"),
        allowed_executors=preserved_raw_data.get("allowed_executors"),
        required_validators=preserved_raw_data.get("required_validators"),
        optional_validators=preserved_raw_data.get("optional_validators", []),
        path_policy=preserved_raw_data.get("path_policy"),
        workspace_policy=preserved_raw_data.get("workspace_policy"),
        proof_of_work=preserved_raw_data.get("proof_of_work"),
        human_review=preserved_raw_data.get("human_review"),
        forbidden_actions=preserved_raw_data.get("forbidden_actions"),
        deferred_integrations=preserved_raw_data.get("deferred_integrations"),
        source_path=source_path,
        raw_data=preserved_raw_data,
    )
