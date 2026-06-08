"""P4-d manual runtime path behind the ExecutionEngine facade.

This module provides the one explicit, opt-in runtime path that runs through the
ExecutionEngine facade. It builds an :class:`ExecutionEngineRequest` from
explicit manual inputs and executes it through
:class:`ApprovedTaskRunnerExecutionEngineAdapter`, which delegates to the
existing ``approved_task_runner.run_approved_task``.

The path is behavior-preserving and opt-in:

    CLI / caller
        -> build_manual_execution_engine_request(...)
        -> ApprovedTaskRunnerExecutionEngineAdapter
        -> approved_task_runner.run_approved_task
        -> ExecutionEngineResult

It does not change the scheduler tick, one-task automation, dispatcher, cron, or
any other existing runtime path. ``build_manual_execution_engine_request`` only
constructs contract dataclasses: it does not touch the filesystem, call git, call
GitHub, or write the DB. ``run_manual_execution_engine_request`` performs no side
effect beyond whatever ``run_approved_task`` already does when the adapter calls
it. Neither helper approves, merges, cleans up, archives, closes out, publishes a
PR, deletes a branch or worktree, closes an issue, mutates GitHub, or starts a
daemon, webhook, background worker, scheduler loop, or multi-task batch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_MANUAL,
    ExecutionEngineExecutorProfile,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    ExecutionEngineValidatorProfile,
    ExecutionEngineWorkspaceProfile,
)


def build_manual_execution_engine_request(
    *,
    task_key: str,
    repo_path: str | Path,
    artifact_dir: str | Path,
    executor: str = "noop",
    validators: Sequence[str] = (),
    dry_run: bool = True,
    preflight: bool = True,
    model: str | None = None,
    provider: str | None = None,
    tools: Sequence[str] = (),
    pi_bin: str | None = None,
    worktree_root: str | Path | None = None,
    runtime_handoff_path: str | Path | None = None,
    verifier_report_path: str | Path | None = None,
    project: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExecutionEngineRequest:
    """Build an :class:`ExecutionEngineRequest` from explicit manual inputs.

    This constructs the executor, validator, and workspace profiles and returns
    an ``ExecutionEngineRequest`` whose ``source`` is ``manual`` and whose
    ``dry_run`` default is ``True``. ``repo_path`` and ``artifact_dir`` are
    required to be absolute; the contract dataclasses enforce this. The function
    does not touch the filesystem, call git, call GitHub, or write the DB.
    """

    executor_profile = ExecutionEngineExecutorProfile(
        executor=executor,
        model=model,
        provider=provider,
        tools=_as_str_tuple(tools),
        pi_bin=pi_bin,
    )
    validator_profile = ExecutionEngineValidatorProfile(
        validators=_as_str_tuple(validators),
    )
    workspace = ExecutionEngineWorkspaceProfile(
        repo_path=repo_path,
        artifact_dir=artifact_dir,
        worktree_root=worktree_root,
    )
    return ExecutionEngineRequest(
        task_key=task_key,
        project=project,
        source=REQUEST_SOURCE_MANUAL,
        dry_run=dry_run,
        preflight=preflight,
        executor_profile=executor_profile,
        validator_profile=validator_profile,
        workspace=workspace,
        runtime_handoff_path=runtime_handoff_path,
        verifier_report_path=verifier_report_path,
        metadata=dict(metadata or {}),
    )


def run_manual_execution_engine_request(
    request: ExecutionEngineRequest,
) -> ExecutionEngineResult:
    """Execute a manual request through the ExecutionEngine facade.

    Instantiates :class:`ApprovedTaskRunnerExecutionEngineAdapter` and returns
    the :class:`ExecutionEngineResult` produced by ``adapter.execute(request)``.
    It performs no extra side effect beyond the adapter execution.
    """

    adapter = ApprovedTaskRunnerExecutionEngineAdapter()
    return adapter.execute(request)


def _as_str_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    """Return a tuple of the provided values, treating ``None`` as empty."""

    if values is None:
        return ()
    if isinstance(values, str):
        return (values,)
    return tuple(values)


__all__ = [
    "build_manual_execution_engine_request",
    "run_manual_execution_engine_request",
]
