"""Validator abstraction for Agent Taskflow.

Validators run checks only inside a verified task worktree and return structured
results that can later be recorded by the dispatcher/store layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from agent_taskflow.context_validation import (
    require_non_empty as _require_non_empty,
    validate_env as _validate_env,
    validate_timeout as _validate_timeout,
)
from agent_taskflow.models import require_absolute_path
from agent_taskflow.tasks import normalize_task_key


VALIDATOR_RESULT_STATUSES = {
    "passed",
    "failed",
    "skipped",
    "blocked",
}


def validate_validator_result_status(status: str) -> str:
    """Return a normalized validator result status or raise ValueError."""
    normalized = _require_non_empty(status, "status")
    if normalized not in VALIDATOR_RESULT_STATUSES:
        raise ValueError(f"Invalid validator result status: {status!r}")
    return normalized


@dataclass(frozen=True)
class ValidatorContext:
    """Runtime context supplied to a validator."""

    task_key: str
    project: str
    worktree_path: Path
    artifact_dir: Path
    timeout_seconds: int | None = None
    env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "project", _require_non_empty(self.project, "project"))
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
        object.__setattr__(
            self,
            "timeout_seconds",
            _validate_timeout(self.timeout_seconds),
        )
        object.__setattr__(self, "env", _validate_env(self.env))


@dataclass(frozen=True)
class ValidatorResult:
    """Structured result returned by a validator."""

    validator: str
    status: str
    exit_code: int | None = None
    log_path: Path | None = None
    summary: str | None = None
    artifacts: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "validator",
            _require_non_empty(self.validator, "validator"),
        )
        object.__setattr__(
            self,
            "status",
            validate_validator_result_status(self.status),
        )

        if self.log_path is not None:
            object.__setattr__(
                self,
                "log_path",
                require_absolute_path(self.log_path, "log_path"),
            )

        normalized_artifacts: dict[str, Path] = {}
        for key, path in self.artifacts.items():
            artifact_key = _require_non_empty(str(key), "artifact key")
            normalized_artifacts[artifact_key] = require_absolute_path(
                path,
                f"artifacts[{artifact_key}]",
            )

        object.__setattr__(self, "artifacts", normalized_artifacts)


class Validator(ABC):
    """Base class for Agent Taskflow validators."""

    name: str

    @abstractmethod
    def run(self, context: ValidatorContext) -> ValidatorResult:
        """Run this validator for the supplied task context."""


__all__ = [
    "VALIDATOR_RESULT_STATUSES",
    "Validator",
    "ValidatorContext",
    "ValidatorResult",
    "validate_validator_result_status",
]
