"""Executor abstraction for Agent Taskflow.

Executors run work only inside a verified task worktree and return structured
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
from agent_taskflow.executor_launch import ExecutorLaunchBinding
from agent_taskflow.models import require_absolute_path
from agent_taskflow.tasks import normalize_task_key


EXECUTOR_RESULT_STATUSES = {
    "completed",
    "failed",
    "blocked",
    "skipped",
}


def validate_executor_result_status(status: str) -> str:
    normalized = _require_non_empty(status, "status")
    if normalized not in EXECUTOR_RESULT_STATUSES:
        raise ValueError(f"Invalid executor result status: {status!r}")
    return normalized


@dataclass(frozen=True)
class ExecutorContext:
    """Runtime context supplied to an executor.

    ``launch_binding`` is injected only by the canonical Attempt runtime. A
    context without this binding preserves the historical direct-subprocess
    behavior for bounded local tools and unit fixtures. A bound context must use
    PR-7's managed launch path.
    """

    task_key: str
    project: str
    worktree_path: Path
    artifact_dir: Path
    prompt_path: Path | None = None
    model: str | None = None
    timeout_seconds: int | None = None
    env: dict[str, str] | None = None
    repo_root: Path | None = None
    launch_binding: ExecutorLaunchBinding | None = None

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

        if self.repo_root is not None:
            object.__setattr__(
                self,
                "repo_root",
                require_absolute_path(self.repo_root, "repo_root"),
            )

        if self.prompt_path is not None:
            object.__setattr__(
                self,
                "prompt_path",
                require_absolute_path(self.prompt_path, "prompt_path"),
            )

        if self.model is not None:
            object.__setattr__(
                self,
                "model",
                _require_non_empty(self.model, "model"),
            )

        object.__setattr__(
            self,
            "timeout_seconds",
            _validate_timeout(self.timeout_seconds),
        )
        object.__setattr__(self, "env", _validate_env(self.env))

        if self.launch_binding is not None:
            binding = self.launch_binding
            if binding.task_key != self.task_key:
                raise ValueError("launch_binding task_key does not match ExecutorContext")
            if binding.worktree_path.resolve() != self.worktree_path.resolve():
                raise ValueError(
                    "launch_binding worktree_path does not match ExecutorContext"
                )
            if binding.artifact_root.resolve() != self.artifact_dir.resolve():
                raise ValueError(
                    "launch_binding artifact_root does not match ExecutorContext"
                )


@dataclass(frozen=True)
class ExecutorResult:
    """Structured result returned by an executor."""

    executor: str
    status: str
    exit_code: int | None = None
    log_path: Path | None = None
    summary: str | None = None
    artifacts: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "executor",
            _require_non_empty(self.executor, "executor"),
        )
        object.__setattr__(
            self,
            "status",
            validate_executor_result_status(self.status),
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


class Executor(ABC):
    """Base class for Agent Taskflow executors."""

    name: str

    @abstractmethod
    def run(self, context: ExecutorContext) -> ExecutorResult:
        """Run this executor for the supplied task context."""


__all__ = [
    "EXECUTOR_RESULT_STATUSES",
    "Executor",
    "ExecutorContext",
    "ExecutorResult",
    "validate_executor_result_status",
]
