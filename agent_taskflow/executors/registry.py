"""Executor registry for built-in Agent Taskflow executors."""

from __future__ import annotations

from typing import Sequence

from agent_taskflow.executors.base import Executor
from agent_taskflow.executors.manual import ManualExecutor, NoopExecutor
from agent_taskflow.executors.shell import ShellExecutor


def _normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("executor name must not be empty")
    return normalized


def get_executor(name: str, *, command: Sequence[str] | None = None) -> Executor:
    """Return a built-in executor by name.

    Shell executors require an explicit command. No external coding workers are
    registered in this phase.
    """

    normalized = _normalize_name(name)

    if normalized == "manual":
        return ManualExecutor()
    if normalized == "noop":
        return NoopExecutor()
    if normalized == "shell":
        if command is None:
            raise ValueError("shell executor requires command")
        return ShellExecutor(command)

    raise ValueError(f"Unknown executor: {name!r}")


def build_shell_executor(command: Sequence[str], *, name: str = "shell") -> ShellExecutor:
    """Build a shell executor with an explicit command."""

    return ShellExecutor(command, name=name)


def list_executor_names() -> list[str]:
    """Return supported executor names."""

    return ["manual", "noop", "shell"]


__all__ = ["build_shell_executor", "get_executor", "list_executor_names"]
