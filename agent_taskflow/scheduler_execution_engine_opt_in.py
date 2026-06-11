"""P5-d: scheduler ExecutionEngine opt-in execution path (off by default).

This module is the first *runtime wiring* stage of the staged
scheduler-to-ExecutionEngine migration plan defined by the P5-a boundary
document (``docs/scheduler-execution-engine-migration-boundary.md``):

* P5-a defined the scheduler-to-ExecutionEngine migration boundary.
* P5-b added a pure scheduler ExecutionEngine request builder
  (``agent_taskflow/scheduler_execution_engine_request_builder.py``).
* P5-c added a pure scheduler ExecutionEngine shadow / compare summary layer
  (``agent_taskflow/scheduler_execution_engine_shadow_compare.py``).
* P5-d (this module) adds an **explicit opt-in** path so a *confirmed* scheduler
  tick can route the one selected task through the ExecutionEngine facade /
  adapter, but only when the new ``--use-execution-engine`` flag is provided.

The default scheduler tick behavior is unchanged: when the opt-in is off, none
of this module runs. The opt-in path is strictly bounded:

* confirmed mode only (dry-run + opt-in is rejected upstream);
* execution-only (``publish_after_execution=False``, ``mode=execution_only``);
* one tick, one selected task, one engine invocation;
* the ExecutionEngine result is **runtime evidence only, not approval
  authority** — deterministic validators and human review gates remain the
  validation and approval authority;
* the shadow / compare result is **diagnostic only**;
* no publish / PR / branch push / draft PR / merge / approval / cleanup /
  archive / closeout / branch or worktree deletion;
* no daemon / webhook / background worker / scheduler loop / multi-task batch.

P5-d is evidence-only instrumentation, **not a live rollout path**: this path
runs after the legacy automation has already executed the selected task and
released the scheduler lock, and the default
:class:`ApprovedTaskRunnerExecutionEngineAdapter` requires the task to still be
queued. A successful legacy run is therefore expected to yield a blocked /
fallback engine candidate rather than a clean one, with the legacy ``ok`` /
``status`` preserved. There is no duplicate execution. See the "expected
post-legacy fallback" section of
``docs/scheduler-execution-engine-opt-in-path.md``.

P5-e (``agent_taskflow/scheduler_execution_engine_fallback.py``) hardens the
legacy-vs-engine fallback semantics of this path: every ``execution_engine``
evidence block carries a pure ``fallback_assessment`` classification pinning
``effective_authority="legacy_scheduler"``, ``engine_authority=False``, and
``engine_result_accepted_as_authority=False``. Rolling the opt-in back is just
removing the opt-in flag: the legacy scheduler tick path is the default and is
never modified by opting out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_SCHEDULED_TICK,
    ExecutionEngine,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    to_json_dict,
)
from agent_taskflow.execution_observability import (
    summarize_execution_engine_result,
    to_observability_dict,
)
from agent_taskflow.scheduler_execution_engine_fallback import (
    EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER,
    SchedulerExecutionEngineFallbackAssessmentInput,
    assess_scheduler_execution_engine_fallback,
    scheduler_execution_engine_fallback_assessment_to_json_dict,
)
from agent_taskflow.scheduler_execution_engine_request_builder import (
    SchedulerExecutionEngineRequestBuildInput,
    build_scheduler_execution_engine_request,
    scheduler_execution_engine_request_to_json_dict,
)
from agent_taskflow.scheduler_execution_engine_shadow_compare import (
    SchedulerExecutionEngineShadowCompareInput,
    compare_scheduler_tick_to_engine_request,
    scheduler_execution_engine_shadow_compare_to_json_dict,
)


SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION = (
    "scheduler_execution_engine_opt_in_path.v1"
)
SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE = (
    "scheduler_execution_engine_opt_in_path"
)

# Default executor recorded on the engine-shaped request when the confirmed tick
# does not configure an explicit executor. ``noop`` is the safe default executor
# used elsewhere in the system.
_DEFAULT_ENGINE_EXECUTOR = "noop"


def build_scheduler_tick_execution_engine_request(
    request: Any,
    *,
    task_key: str,
    selected_issue_number: int | None = None,
    selected_candidate_key: str | None = None,
) -> ExecutionEngineRequest:
    """Build an engine-shaped request for one scheduler-selected confirmed task.

    Pure value mapping over the P5-b builder
    (:func:`build_scheduler_execution_engine_request`). ``request`` is a
    ``GitHubIssueOneTaskSchedulerTickRequest`` (read by attribute only); this
    function touches no filesystem, DB, GitHub, cron, or subprocess.

    The resulting request always carries the scheduled-tick source and the
    execution-only metadata invariants enforced by the P5-b builder:
    ``publish_after_execution=False``, ``mode=execution_only``,
    ``execution_only=True``, ``one_task_only=True``, and ``scheduler_tick=True``.
    """

    builder_input = SchedulerExecutionEngineRequestBuildInput(
        task_key=task_key,
        repo=request.repo,
        local_repo_path=Path(request.local_repo_path),
        artifact_dir=Path(request.artifact_root),
        executor=(request.executor or _DEFAULT_ENGINE_EXECUTOR),
        model=request.model,
        provider=request.provider,
        tools=tuple(request.tools or ()),
        pi_bin=request.pi_bin,
        validators=tuple(request.validators or ()),
        worktree_root=(
            Path(request.worktree_root)
            if request.worktree_root is not None
            else None
        ),
        dry_run=False,
        confirmed=True,
        preflight=bool(request.approved_task_preflight),
        # The scheduler opt-in path is execution-only by construction; the
        # builder rejects any attempt to publish.
        publish_after_execution=False,
        execution_only=True,
        operator=request.operator,
        operator_note=request.operator_note,
        selected_issue_number=selected_issue_number,
        selected_candidate_key=selected_candidate_key,
    )
    return build_scheduler_execution_engine_request(builder_input)


def route_scheduler_tick_through_execution_engine(
    request: Any,
    tick_payload: dict[str, Any],
    *,
    engine: ExecutionEngine | None = None,
) -> dict[str, Any]:
    """Return the ``execution_engine`` opt-in evidence block for one tick.

    This is the single integration point for the P5-d opt-in path. The caller
    (the scheduler tick) invokes it only when ``use_execution_engine`` is set,
    which is itself only valid in confirmed mode. It:

    1. reads the one selected task from ``tick_payload``;
    2. builds the engine-shaped :class:`ExecutionEngineRequest`
       (``source=scheduled_tick``) via the P5-b builder;
    3. produces the P5-c shadow / compare result against the legacy tick payload;
    4. runs the ExecutionEngine facade **exactly once** for the one selected
       task (default: :class:`ApprovedTaskRunnerExecutionEngineAdapter`); and
    5. returns a JSON-compatible evidence block.

    The returned block is evidence only. It never changes the legacy tick
    ``ok`` / ``status`` / publication / safety decisions, and it never publishes,
    opens a PR, pushes or deletes a branch, deletes a worktree, approves, merges,
    cleans up, or starts any background / loop / multi-task behavior. If the
    engine raises or returns a non-ok result, the block records a structured
    failure and the caller does not fall through to any unsafe behavior.

    Every block additionally carries the P5-e ``fallback_assessment``
    classification: ``effective_authority`` is always ``"legacy_scheduler"``,
    ``engine_authority`` is always ``False``, and
    ``engine_result_accepted_as_authority`` is always ``False``. The assessment
    is pure and machine-readable; it never changes the legacy tick decision.
    """

    selected_task_key = _selected_task_key(tick_payload)
    if not selected_task_key:
        return _attach_fallback_assessment(
            _not_executed_block(
                reason="no_selected_task_for_engine_path",
            ),
            tick_payload,
        )

    engine_request = build_scheduler_tick_execution_engine_request(
        request,
        task_key=selected_task_key,
        selected_issue_number=_selected_issue_number(tick_payload),
    )

    # Shadow / compare is produced before engine execution so the diagnostic
    # comparison is always available even if execution fails.
    shadow_compare = compare_scheduler_tick_to_engine_request(
        SchedulerExecutionEngineShadowCompareInput(
            legacy_scheduler_tick=tick_payload,
            engine_request=engine_request,
        )
    )
    shadow_compare_json = scheduler_execution_engine_shadow_compare_to_json_dict(
        shadow_compare
    )

    request_json = scheduler_execution_engine_request_to_json_dict(engine_request)

    active_engine: ExecutionEngine = (
        engine if engine is not None else ApprovedTaskRunnerExecutionEngineAdapter()
    )

    try:
        result = active_engine.execute(engine_request)
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure.
        return _attach_fallback_assessment(
            _engine_error_block(
                task_key=selected_task_key,
                engine=active_engine,
                request_json=request_json,
                shadow_compare_json=shadow_compare_json,
                error=f"{exc.__class__.__name__}: {exc}",
            ),
            tick_payload,
        )

    if not isinstance(result, ExecutionEngineResult):
        return _attach_fallback_assessment(
            _engine_error_block(
                task_key=selected_task_key,
                engine=active_engine,
                request_json=request_json,
                shadow_compare_json=shadow_compare_json,
                error=(
                    "engine returned a non-ExecutionEngineResult value: "
                    f"{type(result).__name__}"
                ),
            ),
            tick_payload,
        )

    return _attach_fallback_assessment(
        _executed_block(
            task_key=selected_task_key,
            engine=active_engine,
            request_json=request_json,
            result=result,
            shadow_compare_json=shadow_compare_json,
        ),
        tick_payload,
    )


def _attach_fallback_assessment(
    block: dict[str, Any],
    tick_payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach the P5-e fallback classification to one evidence block.

    The assessment is a pure value over the legacy tick payload and the
    evidence block. It pins the authority semantics on the block itself —
    ``effective_authority="legacy_scheduler"``, ``engine_authority=False``,
    ``engine_result_accepted_as_authority=False`` — and never changes the
    legacy tick ``ok`` / ``status``.
    """

    assessment = assess_scheduler_execution_engine_fallback(
        SchedulerExecutionEngineFallbackAssessmentInput(
            legacy_tick_payload=tick_payload,
            execution_engine_evidence=block,
        )
    )
    block["fallback_assessment"] = (
        scheduler_execution_engine_fallback_assessment_to_json_dict(assessment)
    )
    block["effective_authority"] = EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER
    block["engine_authority"] = False
    block["engine_result_accepted_as_authority"] = False
    return block


# --------------------------------------------------------------------------
# Evidence-block builders
# --------------------------------------------------------------------------


def _executed_block(
    *,
    task_key: str,
    engine: ExecutionEngine,
    request_json: dict[str, Any],
    result: ExecutionEngineResult,
    shadow_compare_json: dict[str, Any],
) -> dict[str, Any]:
    result_json = _result_to_json(result)
    return {
        "schema_version": SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION,
        "source": SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE,
        "enabled": True,
        "executed": True,
        "confirmed_mode_only": True,
        "mode": "execution_only",
        "engine": type(engine).__name__,
        "engine_invocation_count": 1,
        "ok": bool(result.ok),
        "status": str(result.status),
        "selected_task_key": task_key,
        "request_source": request_json.get("source"),
        "request": request_json,
        "request_summary": _request_summary(request_json),
        "result": result_json,
        "result_summary": _result_summary(result),
        "shadow_compare": shadow_compare_json,
        "observability_summary": _observability_summary(result),
        "safety": _engine_safety(result),
    }


def _engine_error_block(
    *,
    task_key: str,
    engine: ExecutionEngine,
    request_json: dict[str, Any],
    shadow_compare_json: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION,
        "source": SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE,
        "enabled": True,
        "executed": True,
        "confirmed_mode_only": True,
        "mode": "execution_only",
        "engine": type(engine).__name__,
        "engine_invocation_count": 1,
        "ok": False,
        "status": "engine_error",
        "selected_task_key": task_key,
        "request_source": request_json.get("source"),
        "request": request_json,
        "request_summary": _request_summary(request_json),
        "result": None,
        "result_summary": None,
        "shadow_compare": shadow_compare_json,
        "observability_summary": None,
        "error": error,
        "safety": _engine_safety(None),
    }


def _not_executed_block(*, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION,
        "source": SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE,
        "enabled": True,
        "executed": False,
        "confirmed_mode_only": True,
        "mode": "execution_only",
        "engine": None,
        "engine_invocation_count": 0,
        "ok": False,
        "status": "not_executed",
        "selected_task_key": None,
        "request": None,
        "request_summary": None,
        "result": None,
        "result_summary": None,
        "shadow_compare": None,
        "observability_summary": None,
        "reason": reason,
        "safety": _engine_safety(None),
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _selected_task_key(tick_payload: dict[str, Any]) -> str | None:
    value = tick_payload.get("selected_task_key")
    if value is None:
        automation = tick_payload.get("automation")
        if isinstance(automation, dict):
            value = automation.get("selected_task_key")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _selected_issue_number(tick_payload: dict[str, Any]) -> int | None:
    automation = tick_payload.get("automation")
    if not isinstance(automation, dict):
        return None
    selected_issue = automation.get("selected_issue")
    if not isinstance(selected_issue, dict):
        return None
    number = selected_issue.get("number")
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def _result_to_json(result: ExecutionEngineResult) -> dict[str, Any]:
    payload = to_json_dict(result)
    if not isinstance(payload, dict):
        raise TypeError(
            "ExecutionEngineResult did not serialize to a dict: "
            f"{type(payload).__name__}"
        )
    return payload


def _request_summary(request_json: dict[str, Any]) -> dict[str, Any]:
    metadata = request_json.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    executor_profile = request_json.get("executor_profile")
    executor_profile = executor_profile if isinstance(executor_profile, dict) else {}
    return {
        "task_key": request_json.get("task_key"),
        "project": request_json.get("project"),
        "source": request_json.get("source"),
        "dry_run": request_json.get("dry_run"),
        "executor": executor_profile.get("executor"),
        "publish_after_execution": metadata.get("publish_after_execution"),
        "mode": metadata.get("mode"),
        "execution_only": metadata.get("execution_only"),
        "one_task_only": metadata.get("one_task_only"),
        "scheduler_tick": metadata.get("scheduler_tick"),
    }


def _result_summary(result: ExecutionEngineResult) -> dict[str, Any]:
    return {
        "ok": bool(result.ok),
        "task_key": result.task_key,
        "status": str(result.status),
        "summary": result.summary,
        "next_operator_action": result.next_operator_action,
        "step_count": len(result.steps),
    }


def _observability_summary(result: ExecutionEngineResult) -> Any:
    summary = summarize_execution_engine_result(result)
    return to_observability_dict(summary)


def _engine_safety(result: ExecutionEngineResult | None) -> dict[str, Any]:
    """Reaffirm the governance boundary for the engine opt-in path.

    All expansive / destructive markers are recorded ``False`` by construction:
    the opt-in path never approves, merges, cleans up, publishes, pushes or
    deletes a branch, deletes a worktree, or starts any background / loop /
    multi-task behavior. ``github_mutated`` is read from the engine result when
    one exists and otherwise stays ``False``.
    """

    github_mutated = False
    if result is not None:
        github_mutated = bool(getattr(result.safety, "github_mutated", False))
    return {
        "scheduler_tick": True,
        "one_task_only": True,
        "execution_only": True,
        "publish_after_execution": False,
        "approval_authority": False,
        "approved": False,
        "merged": False,
        "github_mutated": github_mutated,
        "branch_pushed": False,
        "draft_pr_created": False,
        "cleanup_performed": False,
        "archived": False,
        "closed_out": False,
        "branch_deleted": False,
        "worktree_deleted": False,
        "daemon_started": False,
        "webhook_started": False,
        "background_worker_started": False,
        "scheduler_loop_started": False,
        "multi_task_batch_started": False,
        "human_review_required": True,
    }


__all__ = [
    "SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION",
    "SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE",
    "build_scheduler_tick_execution_engine_request",
    "route_scheduler_tick_through_execution_engine",
]
