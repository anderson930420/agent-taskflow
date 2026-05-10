"""Built-in executors for Agent Taskflow."""

from agent_taskflow.executors.base import (
    EXECUTOR_RESULT_STATUSES,
    Executor,
    ExecutorContext,
    ExecutorResult,
    validate_executor_result_status,
)
from agent_taskflow.executors.manual import ManualExecutor, NoopExecutor
from agent_taskflow.executors.opencode import OpenCodeExecutor
from agent_taskflow.executors.registry import (
    build_opencode_executor,
    build_shell_executor,
    get_executor,
    list_executor_names,
)
from agent_taskflow.executors.shell import ShellExecutor

__all__ = [
    "EXECUTOR_RESULT_STATUSES",
    "Executor",
    "ExecutorContext",
    "ExecutorResult",
    "ManualExecutor",
    "NoopExecutor",
    "OpenCodeExecutor",
    "ShellExecutor",
    "build_opencode_executor",
    "build_shell_executor",
    "get_executor",
    "list_executor_names",
    "validate_executor_result_status",
]
