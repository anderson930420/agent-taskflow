"""Executor abstractions and built-in executors for Agent Taskflow."""

from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.executors.manual import ManualExecutor, NoopExecutor
from agent_taskflow.executors.registry import (
    build_shell_executor,
    get_executor,
    list_executor_names,
)
from agent_taskflow.executors.shell import ShellExecutor

__all__ = [
    "Executor",
    "ExecutorContext",
    "ExecutorResult",
    "ManualExecutor",
    "NoopExecutor",
    "ShellExecutor",
    "build_shell_executor",
    "get_executor",
    "list_executor_names",
]
