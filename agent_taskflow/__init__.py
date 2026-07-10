"""Agent Taskflow shared Python package."""

from __future__ import annotations

from importlib import import_module

_dispatcher_module = import_module("agent_taskflow.dispatcher")
_approved_task_runner_module = import_module("agent_taskflow.approved_task_runner")
_runtime_admission_module = import_module("agent_taskflow.runtime_admission")

from agent_taskflow.canonical_runtime_path import install_canonical_runtime_path

install_canonical_runtime_path(
    dispatcher_module=_dispatcher_module,
    approved_task_runner_module=_approved_task_runner_module,
    runtime_admission_module=_runtime_admission_module,
)

DEFAULT_VALIDATORS = _dispatcher_module.DEFAULT_VALIDATORS
Dispatcher = _dispatcher_module.Dispatcher
DispatcherResult = _dispatcher_module.DispatcherResult
dispatch_task = _dispatcher_module.dispatch_task

__all__ = [
    "artifacts",
    "config",
    "dispatcher",
    "executors",
    "governance",
    "models",
    "projects",
    "store",
    "tasks",
    "validators",
    "worktree",
    "DEFAULT_VALIDATORS",
    "Dispatcher",
    "DispatcherResult",
    "dispatch_task",
]
