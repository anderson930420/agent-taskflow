"""One-shot approved task runner for Agent Taskflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from agent_taskflow.api.schemas import json_safe
from agent_taskflow.codex_advisory_evidence_gate import (
    RequiredCodexAdvisoryEvidenceRequest,
    RequiredCodexAdvisoryEvidenceResult,
    check_required_codex_advisory_evidence,
)
from agent_taskflow.dispatcher import DEFAULT_VALIDATORS
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.executors.implementation_prompt import (
    EXECUTORS_REQUIRING_PROMPT,
    IMPLEMENTATION_PROMPT_FILENAME,
    render_implementation_prompt,
)
from agent_taskflow.executors.registry import build_shell_executor, get_executor, list_executor_names
from agent_taskflow.github_issue_ingestion import ISSUE_SPEC_FILENAME
from agent_taskflow.mission_contract import build_from_task_fields, write_mission_contract
from agent_taskflow.models import TaskRecord, require_absolute_path
from agent_taskflow.preflight import PreflightResult, run_preflight
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult
from agent_taskflow.validators.registry import get_validator
from agent_taskflow.workspace_manager import (
    WorkspacePreparationRequest,
    WorkspacePreparationResult,
    prepare_task_workspace,
)


APPROVED_TASK_STATUS = "waiting_approval"
TASK_QUEUE_STATUS = "queued"
RUN_STATUS_PREPARING = "preparing"
RUN_STATUS_IMPLEMENTING = "implementing"
RUN_STATUS_VALIDATING = "validating"
RUN_STATUS_BLOCKED = "blocked"
PHASE_CODEX_ADVISORY_EVIDENCE = "codex_advisory_evidence"
DEFAULT_BASE_BRANCH = "main"
SUPPORTED_EXECUTORS = tuple(list_executor_names())
BUILTIN_VALIDATORS = {"pytest", "openspec", "policy", "changed-files", "typecheck", "lint"}
# Executors that cannot run without an explicit model in their profile.
EXECUTORS_REQUIRING_MODEL = frozenset({"opencode"})


class ApprovedTaskRunnerError(RuntimeError):
    """Raised when an approved task cannot proceed."""


def _normalize_validators(validators: Sequence[str] | None) -> tuple[str, ...]:
    if validators is None:
        return DEFAULT_VALIDATORS
    normalized = tuple(value.strip().lower() for value in validators if value.strip())
    return normalized or DEFAULT_VALIDATORS


@dataclass(frozen=True)
class ApprovedTaskRunRequest:
    """Input for a one-shot approved task run."""

    task_key: str
    executor: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    worktree_root: Path | None = None
    base_branch: str = DEFAULT_BASE_BRANCH
    validators: tuple[str, ...] = DEFAULT_VALIDATORS
    confirm_approved_task: bool = False
    dry_run: bool = False
    preflight: bool = True
    # v0.2.5: require valid Codex advisory artifact contract evidence before a
    # task may transition into waiting_approval. This requires advisory evidence,
    # not Codex approval; the deterministic contract validator must pass.
    require_codex_advisory_evidence: bool = True
    command: tuple[str, ...] | None = None
    # Executor profile overrides. When provided these take precedence over the
    # TaskRecord profile fields; otherwise the recorded TaskRecord profile is
    # used. Real executors such as opencode/pi may require this configuration.
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] | None = None
    pi_bin: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        executor = self.executor.strip().lower()
        if not executor:
            raise ValueError("executor must not be empty")
        object.__setattr__(self, "executor", executor)
        object.__setattr__(self, "repo_path", require_absolute_path(self.repo_path, "repo_path"))
        if self.db_path is not None:
            object.__setattr__(self, "db_path", require_absolute_path(self.db_path, "db_path"))
        if self.artifact_root is not None:
            object.__setattr__(self, "artifact_root", require_absolute_path(self.artifact_root, "artifact_root"))
        if self.worktree_root is not None:
            object.__setattr__(self, "worktree_root", require_absolute_path(self.worktree_root, "worktree_root"))
        base_branch = self.base_branch.strip()
        if not base_branch:
            raise ValueError("base_branch must not be empty")
        object.__setattr__(self, "base_branch", base_branch)
        object.__setattr__(self, "validators", _normalize_validators(self.validators))
        if self.command is not None:
            command = tuple(part.strip() for part in self.command if str(part).strip())
            if not command:
                raise ValueError("command must not be empty when provided")
            object.__setattr__(self, "command", command)
        for field_name in ("model", "provider", "pi_bin"):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = str(value).strip()
            object.__setattr__(self, field_name, stripped or None)
        if self.tools is not None:
            tools = tuple(part.strip() for part in self.tools if str(part).strip())
            object.__setattr__(self, "tools", tools or None)


@dataclass(frozen=True)
class ApprovedTaskRunResult:
    """Structured result for a one-shot approved task run."""

    ok: bool
    status: str
    phase: str
    task_key: str
    executor: str
    dry_run: bool
    preflight: dict[str, Any]
    workspace: dict[str, Any]
    executor_run: dict[str, Any]
    validators: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    summary: dict[str, Any]
    safety: dict[str, Any]
    error: str | None = None
    codex_advisory_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


def run_approved_task(
    request: ApprovedTaskRunRequest,
    *,
    store: TaskMirrorStore | None = None,
    executor_registry: Mapping[str, Executor] | None = None,
    validator_registry: Mapping[str, Validator] | None = None,
    preflight_runner=run_preflight,
) -> ApprovedTaskRunResult:
    """Run one explicitly approved queued task and stop at human review."""

    current_store = store or TaskMirrorStore(request.db_path)
    executor_registry = dict(executor_registry or {})
    validator_registry = dict(validator_registry or {})

    selection_error = _validate_selection(
        request,
        executor_registry=executor_registry,
        validator_registry=validator_registry,
    )
    if selection_error is not None:
        return _blocked_preview(request, phase="selection", error=selection_error)

    if not request.dry_run and not request.confirm_approved_task:
        return _blocked_preview(
            request,
            phase="confirmation",
            error="Approved task runner requires --confirm-approved-task",
        )

    task = _load_task(current_store, request)
    if task is None:
        return _blocked_preview(request, phase="selection", error=f"Task not found: {request.task_key}")
    if task.status != TASK_QUEUE_STATUS:
        return _blocked_preview(
            request,
            phase="selection",
            error=f"Task {task.task_key} must be queued before approved execution, got {task.status}",
        )

    repo_error = _validate_repo(task, request)
    if repo_error is not None:
        if not request.dry_run:
            _block_task(current_store, task.task_key, repo_error)
        return _blocked_failure(
            request,
            task=task,
            phase="selection",
            error=repo_error,
            task_status_changed=not request.dry_run,
        )

    effective_artifact_dir = _effective_artifact_dir(task, request)
    effective_task = replace(
        task,
        artifact_dir=effective_artifact_dir,
        executor=request.executor,
        model=request.model if request.model is not None else task.model,
        provider=request.provider if request.provider is not None else task.provider,
        tools=list(request.tools) if request.tools is not None else task.tools,
        pi_bin=request.pi_bin if request.pi_bin is not None else task.pi_bin,
    )
    preflight_result = _run_preflight(request, preflight_runner=preflight_runner)
    preflight_payload = _preflight_payload(preflight_result, ran=preflight_result is not None)

    if request.dry_run:
        if preflight_result is not None and not preflight_result.ok:
            return _blocked_failure(
                request,
                task=effective_task,
                phase="preflight",
                error=_preflight_failure_summary(preflight_result),
                task_status_changed=False,
                preflight=preflight_payload,
            )
        return _dry_run_result(request, task=effective_task, preflight=preflight_payload)

    current_store.init_db()
    if effective_task.artifact_dir is None:
        reason = "Task artifact_dir is required or artifact_root must be provided"
        _block_task(current_store, effective_task.task_key, reason)
        return _blocked_failure(
            request,
            task=effective_task,
            phase="selection",
            error=reason,
            task_status_changed=True,
            preflight=preflight_payload,
        )

    if effective_task != task:
        current_store.upsert_task(effective_task)

    if preflight_result is not None and not preflight_result.ok:
        preflight_error = _preflight_failure_summary(preflight_result)
        _block_task(current_store, effective_task.task_key, preflight_error)
        return _blocked_failure(
            request,
            task=effective_task,
            phase="preflight",
            error=preflight_error,
            task_status_changed=True,
            preflight=preflight_payload,
        )

    current_store.update_task_status(
        effective_task.task_key,
        RUN_STATUS_PREPARING,
        source="approved_task_runner",
        message="Approved task runner preparing workspace",
    )

    workspace_request = WorkspacePreparationRequest(
        task_key=effective_task.task_key,
        repo_path=effective_task.repo_path,
        base_branch=request.base_branch,
        worktree_root=request.worktree_root,
    )
    workspace_result = prepare_task_workspace(workspace_request, store=current_store)
    if not workspace_result.ok:
        _block_task(current_store, effective_task.task_key, workspace_result.summary)
        return _blocked_failure(
            request,
            task=effective_task,
            phase="workspace",
            error=workspace_result.summary,
            task_status_changed=True,
            preflight=preflight_payload,
            workspace=_workspace_payload(workspace_result),
        )

    contract_path = _write_mission_contract(effective_task, workspace_result, validators=request.validators)
    _record_artifact(current_store, effective_task.task_key, "manifest", contract_path)

    executor = _resolve_executor(request, effective_task, executor_registry=executor_registry)
    executor_context = _build_executor_context(effective_task, workspace_result)
    model_requirement_error = _model_requirement_error(request, effective_task)
    if model_requirement_error is not None:
        _block_task(current_store, effective_task.task_key, model_requirement_error)
        return _blocked_failure(
            request,
            task=effective_task,
            phase="executor",
            error=model_requirement_error,
            task_status_changed=True,
            preflight=preflight_payload,
            workspace=_workspace_payload(workspace_result),
        )
    if request.executor in EXECUTORS_REQUIRING_PROMPT and executor_context.prompt_path is None:
        prompt_path, prompt_error = _ensure_implementation_prompt(effective_task)
        if prompt_error is not None:
            _block_task(current_store, effective_task.task_key, prompt_error)
            return _blocked_failure(
                request,
                task=effective_task,
                phase="executor",
                error=prompt_error,
                task_status_changed=True,
                preflight=preflight_payload,
                workspace=_workspace_payload(workspace_result),
            )
        assert prompt_path is not None
        _record_artifact(current_store, effective_task.task_key, "implementation_prompt", prompt_path)
        executor_context = replace(executor_context, prompt_path=prompt_path)

    executor_run_id = current_store.create_executor_run(
        effective_task.task_key,
        request.executor,
        model=executor_context.model,
        prompt_path=executor_context.prompt_path,
    )

    current_store.update_task_status(
        effective_task.task_key,
        RUN_STATUS_IMPLEMENTING,
        source="approved_task_runner",
        message=f"Approved task runner running executor {request.executor}",
    )

    try:
        executor_result = executor.run(executor_context)
    except Exception as exc:  # pragma: no cover - defensive runtime failure path.
        reason = f"Executor {request.executor} raised {exc.__class__.__name__}: {exc}"
        current_store.finish_executor_run(
            effective_task.task_key,
            executor_run_id,
            executor=request.executor,
            status=RUN_STATUS_BLOCKED,
            summary=reason,
        )
        _block_task(current_store, effective_task.task_key, reason)
        return _blocked_failure(
            request,
            task=effective_task,
            phase="executor",
            error=reason,
            task_status_changed=True,
            preflight=preflight_payload,
            workspace=_workspace_payload(workspace_result),
            executor_run=_executor_run_payload(
                executor_run_id,
                request.executor,
                executor_result=None,
                started=True,
                finished=True,
                ok=False,
                summary=reason,
            ),
            artifacts=_collect_artifacts(
                current_store,
                effective_task.task_key,
                contract_path=contract_path,
                executor_result=None,
                validation_results=[],
            ),
        )

    current_store.finish_executor_run(
        effective_task.task_key,
        executor_run_id,
        executor=executor_result.executor,
        status=executor_result.status,
        exit_code=executor_result.exit_code,
        summary=executor_result.summary,
        log_path=executor_result.log_path,
        artifacts=executor_result.artifacts,
    )
    _record_executor_artifacts(current_store, effective_task.task_key, executor_result)

    if executor_result.status in {"failed", "blocked"}:
        reason = executor_result.summary or f"Executor {request.executor} returned {executor_result.status}"
        _block_task(current_store, effective_task.task_key, reason)
        return _blocked_failure(
            request,
            task=effective_task,
            phase="executor",
            error=reason,
            task_status_changed=True,
            preflight=preflight_payload,
            workspace=_workspace_payload(workspace_result),
            executor_run=_executor_run_payload(
                executor_run_id,
                request.executor,
                executor_result=executor_result,
                started=True,
                finished=True,
                ok=False,
            ),
            artifacts=_collect_artifacts(
                current_store,
                effective_task.task_key,
                contract_path=contract_path,
                executor_result=executor_result,
                validation_results=[],
            ),
        )

    current_store.update_task_status(
        effective_task.task_key,
        RUN_STATUS_VALIDATING,
        source="approved_task_runner",
        message="Approved task runner running validators",
    )

    validator_results: list[ValidatorResult] = []
    for validator_name in request.validators:
        validator = _resolve_validator(validator_name, validator_registry=validator_registry)
        validator_context = ValidatorContext(
            task_key=effective_task.task_key,
            project=effective_task.project,
            worktree_path=workspace_result.worktree_path,
            artifact_dir=effective_task.artifact_dir,
        )
        try:
            validator_result = validator.run(validator_context)
        except Exception as exc:  # pragma: no cover - defensive runtime failure path.
            reason = f"Validator {validator_name} raised {exc.__class__.__name__}: {exc}"
            current_store.record_validation_result(
                effective_task.task_key,
                validator_name,
                status=RUN_STATUS_BLOCKED,
                summary=reason,
            )
            _block_task(current_store, effective_task.task_key, reason)
            return _blocked_failure(
                request,
                task=effective_task,
                phase="validation",
                error=reason,
                task_status_changed=True,
                preflight=preflight_payload,
                workspace=_workspace_payload(workspace_result),
                executor_run=_executor_run_payload(
                    executor_run_id,
                    request.executor,
                    executor_result=executor_result,
                    started=True,
                    finished=True,
                    ok=True,
                ),
                validators=[_validator_payload(item) for item in validator_results],
                artifacts=_collect_artifacts(
                    current_store,
                    effective_task.task_key,
                    contract_path=contract_path,
                    executor_result=executor_result,
                    validation_results=validator_results,
                ),
            )

        validator_results.append(validator_result)
        current_store.record_validation_result(
            effective_task.task_key,
            validator_result.validator,
            status=validator_result.status,
            exit_code=validator_result.exit_code,
            summary=validator_result.summary,
            log_path=validator_result.log_path,
            artifacts=validator_result.artifacts,
        )
        _record_validator_artifacts(current_store, effective_task.task_key, validator_result)

        if validator_result.status in {"failed", "blocked"}:
            reason = validator_result.summary or f"Validator {validator_result.validator} returned {validator_result.status}"
            _block_task(current_store, effective_task.task_key, reason)
            return _blocked_failure(
                request,
                task=effective_task,
                phase="validation",
                error=reason,
                task_status_changed=True,
                preflight=preflight_payload,
                workspace=_workspace_payload(workspace_result),
                executor_run=_executor_run_payload(
                    executor_run_id,
                    request.executor,
                    executor_result=executor_result,
                    started=True,
                    finished=True,
                    ok=True,
                ),
                validators=[_validator_payload(item) for item in validator_results],
                artifacts=_collect_artifacts(
                    current_store,
                    effective_task.task_key,
                    contract_path=contract_path,
                    executor_result=executor_result,
                    validation_results=validator_results,
                ),
            )

    # v0.2.5 pre-waiting-approval gate: deterministic validators have passed, but
    # the task may only enter waiting_approval when valid Codex advisory artifact
    # contract evidence is also present. This requires advisory evidence, not
    # Codex approval; review_status looks_good/needs_attention/high_risk and a
    # structurally valid tool_error are all acceptable evidence.
    evidence_result = _check_codex_advisory_evidence(request, effective_task)
    if evidence_result is not None and not evidence_result.satisfied:
        reason = evidence_result.blocking_summary()
        _block_task(current_store, effective_task.task_key, reason)
        return _blocked_failure(
            request,
            task=effective_task,
            phase=PHASE_CODEX_ADVISORY_EVIDENCE,
            error=reason,
            task_status_changed=True,
            preflight=preflight_payload,
            workspace=_workspace_payload(workspace_result),
            executor_run=_executor_run_payload(
                executor_run_id,
                request.executor,
                executor_result=executor_result,
                started=True,
                finished=True,
                ok=True,
            ),
            validators=[_validator_payload(item) for item in validator_results],
            artifacts=_collect_artifacts(
                current_store,
                effective_task.task_key,
                contract_path=contract_path,
                executor_result=executor_result,
                validation_results=validator_results,
            ),
            codex_advisory_evidence=evidence_result.to_dict(),
        )

    current_store.update_task_status(
        effective_task.task_key,
        APPROVED_TASK_STATUS,
        source="approved_task_runner",
        message="Approved task runner completed implementation and validation",
    )

    return ApprovedTaskRunResult(
        ok=True,
        status=APPROVED_TASK_STATUS,
        phase=APPROVED_TASK_STATUS,
        task_key=effective_task.task_key,
        executor=request.executor,
        dry_run=False,
        preflight=preflight_payload,
        workspace=_workspace_payload(workspace_result),
        executor_run=_executor_run_payload(
            executor_run_id,
            request.executor,
            executor_result=executor_result,
            started=True,
            finished=True,
            ok=True,
        ),
        validators=[_validator_payload(item) for item in validator_results],
        artifacts=_collect_artifacts(
            current_store,
            effective_task.task_key,
            contract_path=contract_path,
            executor_result=executor_result,
            validation_results=validator_results,
        ),
        codex_advisory_evidence=(
            evidence_result.to_dict() if evidence_result is not None else {}
        ),
        summary={
            "final_task_status": APPROVED_TASK_STATUS,
            "requires_human_review": True,
            "next_allowed_phase": "waiting_approval_handoff",
            "task_key": effective_task.task_key,
            "executor": request.executor,
            "codex_advisory_evidence_required": (
                request.require_codex_advisory_evidence
            ),
            "codex_advisory_evidence_satisfied": (
                evidence_result.satisfied if evidence_result is not None else None
            ),
        },
        safety=_final_safety(
            request,
            task_status_changed=True,
            workspace_prepared=True,
            executor_started=True,
            validators_started=True,
            artifact_written=True,
            db_written=True,
            read_only=False,
        ),
    )


def _validate_selection(
    request: ApprovedTaskRunRequest,
    *,
    executor_registry: Mapping[str, Executor],
    validator_registry: Mapping[str, Validator],
) -> str | None:
    if request.executor not in SUPPORTED_EXECUTORS:
        return f"Unknown executor: {request.executor}"

    if request.command is not None and request.executor != "shell":
        return "command may only be provided when executor is shell"

    if request.executor == "shell" and request.command is None:
        return "shell executor requires --command"

    return _validate_validator_names(request, validator_registry=validator_registry)


def _validate_validator_names(
    request: ApprovedTaskRunRequest,
    *,
    validator_registry: Mapping[str, Validator],
) -> str | None:
    for validator_name in request.validators:
        if validator_name in validator_registry:
            continue
        if validator_name not in BUILTIN_VALIDATORS:
            return f"Unknown validator: {validator_name}"
        try:
            get_validator(validator_name)
        except Exception as exc:
            return f"Unknown validator: {validator_name}: {exc}"
    return None


def _load_task(store: TaskMirrorStore, request: ApprovedTaskRunRequest) -> TaskRecord | None:
    if request.dry_run and request.db_path is not None and not request.db_path.exists():
        return None
    if not request.dry_run:
        store.init_db()
    try:
        return store.get_task(request.task_key)
    except Exception as exc:  # pragma: no cover - defensive read path.
        raise ApprovedTaskRunnerError(f"Task record is invalid: {exc}") from exc


def _validate_repo(task: TaskRecord, request: ApprovedTaskRunRequest) -> str | None:
    if task.repo_path.resolve() != request.repo_path.resolve():
        return f"repo_path mismatch for task {task.task_key}: {task.repo_path} != {request.repo_path}"

    repo_root = _git(["rev-parse", "--show-toplevel"], request.repo_path)
    if repo_root.returncode != 0:
        return f"repo_path is not a git repository: {request.repo_path}: {repo_root.stderr.strip() or repo_root.stdout.strip()}"
    if Path(repo_root.stdout.strip()).resolve() != request.repo_path.resolve():
        return f"repo_path must be the git repository root: {request.repo_path}"

    base = _git(["rev-parse", request.base_branch], request.repo_path)
    if base.returncode != 0:
        return f"base ref could not be resolved: {request.base_branch}: {base.stderr.strip() or base.stdout.strip()}"
    return None


def _run_preflight(request: ApprovedTaskRunRequest, *, preflight_runner) -> PreflightResult | None:
    if not request.preflight:
        return None
    return preflight_runner(
        validators=request.validators,
        executor=request.executor,
        require_pytest="pytest" in request.validators,
        require_pi=request.executor == "pi",
        require_opencode=request.executor == "opencode",
        repo_root=request.repo_path,
    )


def _resolve_executor(
    request: ApprovedTaskRunRequest,
    task: TaskRecord,
    *,
    executor_registry: Mapping[str, Executor],
) -> Executor:
    if request.executor in executor_registry:
        return executor_registry[request.executor]
    if request.executor == "shell":
        assert request.command is not None
        return build_shell_executor(request.command, name="shell")
    return get_executor(
        request.executor,
        model=task.model,
        provider=task.provider,
        tools=task.tools if task.tools else None,
        pi_bin=task.pi_bin if task.pi_bin else "pi",
    )


def _model_requirement_error(request: ApprovedTaskRunRequest, task: TaskRecord) -> str | None:
    if request.executor not in EXECUTORS_REQUIRING_MODEL:
        return None
    if request.model or task.model:
        return None
    return (
        f"{request.executor} executor requires a model; provide --model or "
        "record a model in the task executor profile"
    )


def _resolve_validator(validator_name: str, *, validator_registry: Mapping[str, Validator]) -> Validator:
    if validator_name in validator_registry:
        return validator_registry[validator_name]
    return get_validator(validator_name)


def _block_task(store: TaskMirrorStore, task_key: str, reason: str) -> None:
    store.update_task_status(
        task_key,
        RUN_STATUS_BLOCKED,
        source="approved_task_runner",
        message=reason,
        blocked_reason=reason,
    )


def _preflight_failure_summary(result: PreflightResult) -> str:
    for check in result.checks:
        if check.status in {"failed", "warning"} and check.summary:
            return check.summary
    if result.missing_required:
        return ", ".join(result.missing_required)
    if result.missing_optional:
        return ", ".join(result.missing_optional)
    return result.status or "preflight failed"


def _dry_run_result(
    request: ApprovedTaskRunRequest,
    *,
    task: TaskRecord,
    preflight: dict[str, Any],
) -> ApprovedTaskRunResult:
    return ApprovedTaskRunResult(
        ok=True,
        status="preview",
        phase="preview",
        task_key=task.task_key,
        executor=request.executor,
        dry_run=True,
        preflight=preflight,
        workspace=_workspace_preview(request, task.task_key),
        executor_run={
            "started": False,
            "finished": False,
            "ok": None,
            "run_id": None,
            "executor": request.executor,
            "status": None,
            "summary": "Executor was not run.",
            "log_path": None,
        },
        validators=[],
        artifacts=[],
        summary={
            "mode": "dry_run",
            "final_task_status": task.status,
            "would_final_task_status": APPROVED_TASK_STATUS,
            "requires_human_review": True,
            "next_allowed_phase": "approved_task_confirmation",
            "task_key": task.task_key,
            "executor": request.executor,
        },
        safety=_final_safety(
            request,
            task_status_changed=False,
            workspace_prepared=False,
            executor_started=False,
            validators_started=False,
            artifact_written=False,
            db_written=False,
            read_only=True,
        ),
    )


def _blocked_preview(
    request: ApprovedTaskRunRequest,
    *,
    phase: str,
    error: str,
) -> ApprovedTaskRunResult:
    return ApprovedTaskRunResult(
        ok=False,
        status=RUN_STATUS_BLOCKED,
        phase=phase,
        task_key=request.task_key,
        executor=request.executor,
        dry_run=request.dry_run,
        preflight=_preflight_payload(None, ran=False),
        workspace=_workspace_preview(request, request.task_key),
        executor_run={
            "started": False,
            "finished": False,
            "ok": False,
            "run_id": None,
            "executor": request.executor,
            "status": None,
            "summary": "Executor was not run.",
            "log_path": None,
        },
        validators=[],
        artifacts=[],
        summary={
            "final_task_status": RUN_STATUS_BLOCKED,
            "requires_human_review": True,
            "next_allowed_phase": "operator_review",
            "task_key": request.task_key,
            "executor": request.executor,
        },
        safety=_final_safety(
            request,
            task_status_changed=False,
            workspace_prepared=False,
            executor_started=False,
            validators_started=False,
            artifact_written=False,
            db_written=False,
            read_only=True,
        ),
        error=error,
    )


def _blocked_failure(
    request: ApprovedTaskRunRequest,
    *,
    task: TaskRecord,
    phase: str,
    error: str,
    task_status_changed: bool,
    preflight: dict[str, Any] | None = None,
    workspace: dict[str, Any] | None = None,
    executor_run: dict[str, Any] | None = None,
    validators: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    codex_advisory_evidence: dict[str, Any] | None = None,
) -> ApprovedTaskRunResult:
    payload_workspace = workspace if workspace is not None else _workspace_preview(request, task.task_key)
    payload_executor_run = (
        executor_run
        if executor_run is not None
        else {
            "started": False,
            "finished": False,
            "ok": False,
            "run_id": None,
            "executor": request.executor,
            "status": None,
            "summary": "Executor was not run.",
            "log_path": None,
        }
    )
    payload_validators = validators or []
    payload_artifacts = artifacts or []
    artifact_written = bool(payload_artifacts)
    return ApprovedTaskRunResult(
        ok=False,
        status=RUN_STATUS_BLOCKED,
        phase=phase,
        task_key=task.task_key,
        executor=request.executor,
        dry_run=False,
        preflight=preflight if preflight is not None else _preflight_payload(None, ran=False),
        workspace=payload_workspace,
        executor_run=payload_executor_run,
        validators=payload_validators,
        artifacts=payload_artifacts,
        summary={
            "final_task_status": RUN_STATUS_BLOCKED,
            "requires_human_review": True,
            "next_allowed_phase": "operator_review",
            "task_key": task.task_key,
            "executor": request.executor,
        },
        safety=_final_safety(
            request,
            task_status_changed=task_status_changed,
            workspace_prepared=workspace is not None and bool(payload_workspace.get("prepared")),
            executor_started=payload_executor_run.get("started", False),
            validators_started=bool(payload_validators),
            artifact_written=artifact_written,
            db_written=task_status_changed or artifact_written,
            read_only=False,
        ),
        error=error,
        codex_advisory_evidence=codex_advisory_evidence or {},
    )


def _check_codex_advisory_evidence(
    request: ApprovedTaskRunRequest,
    task: TaskRecord,
) -> RequiredCodexAdvisoryEvidenceResult | None:
    """Check required Codex advisory artifact evidence before waiting_approval.

    Returns ``None`` when the requirement is explicitly disabled. Otherwise it
    returns the deterministic evidence gate result. This reads files only; it
    never invokes Codex, runs a subprocess, or mutates state.
    """

    if not request.require_codex_advisory_evidence:
        return None
    artifact_dir = task.artifact_dir
    if artifact_dir is None:  # pragma: no cover - guarded earlier in the runner.
        artifact_dir = Path(".")
    return check_required_codex_advisory_evidence(
        RequiredCodexAdvisoryEvidenceRequest(
            artifact_dir=artifact_dir,
            task_key=task.task_key,
        )
    )


def _final_safety(
    request: ApprovedTaskRunRequest,
    *,
    task_status_changed: bool,
    workspace_prepared: bool,
    executor_started: bool,
    validators_started: bool,
    artifact_written: bool = False,
    db_written: bool | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    if db_written is None:
        db_written = task_status_changed or artifact_written
    return {
        "read_only": read_only,
        "human_approval_required": True,
        "human_approval_confirmed": request.confirm_approved_task,
        "auto_selected_task": False,
        "task_status_changed": task_status_changed,
        "db_written": db_written,
        "artifact_written": artifact_written,
        "workspace_prepared": workspace_prepared,
        "executor_started": executor_started,
        "validators_started": validators_started,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _ensure_implementation_prompt(task: TaskRecord) -> tuple[Path | None, str | None]:
    """Generate a deterministic implementation prompt from the issue spec.

    Returns ``(prompt_path, None)`` when the prompt already exists or was
    generated from ``issue_spec.md``, and ``(None, reason)`` when the issue spec
    needed to generate it is missing so the caller can block the task. It writes
    only the prompt file and records nothing about approval, merge, push, or
    cleanup; the runner remains the artifact and review authority.
    """

    artifact_dir = task.artifact_dir
    if artifact_dir is None:  # pragma: no cover - guarded before the executor phase.
        return None, "Task artifact_dir is required to generate implementation_prompt.md"
    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
    if prompt_path.exists():
        return prompt_path, None
    issue_spec_path = artifact_dir / ISSUE_SPEC_FILENAME
    if not issue_spec_path.exists():
        return None, (
            "issue_spec.md is required to generate implementation_prompt.md for "
            f"{task.executor or 'opencode'} executor: {issue_spec_path}"
        )
    issue_spec_text = issue_spec_path.read_text(encoding="utf-8")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        render_implementation_prompt(
            task_key=task.task_key,
            title=task.title,
            issue_spec=issue_spec_text,
        ),
        encoding="utf-8",
    )
    return prompt_path, None


def _build_executor_context(task: TaskRecord, workspace_result: WorkspacePreparationResult) -> ExecutorContext:
    artifact_dir = task.artifact_dir or workspace_result.worktree_path
    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
    if not prompt_path.exists():
        prompt_path = None
    return ExecutorContext(
        task_key=task.task_key,
        project=task.project,
        worktree_path=workspace_result.worktree_path,
        artifact_dir=artifact_dir,
        prompt_path=prompt_path,
        model=task.model,
        repo_root=task.repo_path,
    )


def _write_mission_contract(
    task: TaskRecord,
    workspace_result: WorkspacePreparationResult,
    *,
    validators: Sequence[str],
) -> Path:
    artifact_dir = task.artifact_dir
    if artifact_dir is None:
        raise ApprovedTaskRunnerError("Task artifact_dir is required")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    contract = build_from_task_fields(
        task_key=task.task_key,
        goal=task.title or f"Task {task.task_key}",
        repo_path=task.repo_path,
        worktree_path=workspace_result.worktree_path,
        artifact_dir=artifact_dir,
        executor=task.executor or "manual",
        model=task.model,
        provider=task.provider,
        required_validators=tuple(validators),
        implementation_prompt_path=artifact_dir / IMPLEMENTATION_PROMPT_FILENAME,
    )
    return write_mission_contract(contract, artifact_dir=artifact_dir)


def _effective_artifact_dir(task: TaskRecord, request: ApprovedTaskRunRequest) -> Path:
    if request.artifact_root is not None:
        return request.artifact_root / task.task_key
    if task.artifact_dir is None:
        raise ApprovedTaskRunnerError("Task artifact_dir is required")
    return task.artifact_dir


def _record_artifact(store: TaskMirrorStore, task_key: str, artifact_type: str, path: Path) -> None:
    existing = {(record.artifact_type, str(record.path)) for record in store.list_task_artifacts(task_key)}
    key = (artifact_type, str(path))
    if key in existing:
        return
    store.record_task_artifact(task_key, artifact_type, path)


def _record_executor_artifacts(store: TaskMirrorStore, task_key: str, result: ExecutorResult) -> None:
    if result.log_path is not None:
        _record_artifact(store, task_key, "worker_log", result.log_path)
    for artifact_path in result.artifacts.values():
        _record_artifact(store, task_key, "other", artifact_path)


def _record_validator_artifacts(store: TaskMirrorStore, task_key: str, result: ValidatorResult) -> None:
    if result.log_path is not None:
        _record_artifact(store, task_key, "review_log", result.log_path)
    for artifact_path in result.artifacts.values():
        _record_artifact(store, task_key, "other", artifact_path)


def _collect_artifacts(
    store: TaskMirrorStore,
    task_key: str,
    *,
    contract_path: Path,
    executor_result: ExecutorResult | None,
    validation_results: Sequence[ValidatorResult],
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    if contract_path.exists():
        artifacts.append({"kind": "mission_contract", "path": str(contract_path)})
    if executor_result is not None:
        if executor_result.log_path is not None:
            artifacts.append({"kind": "executor_log", "path": str(executor_result.log_path)})
        for artifact_path in executor_result.artifacts.values():
            artifacts.append({"kind": "executor_artifact", "path": str(artifact_path)})
    for validator_result in validation_results:
        if validator_result.log_path is not None:
            artifacts.append({"kind": "validator_log", "path": str(validator_result.log_path)})
        for artifact_path in validator_result.artifacts.values():
            artifacts.append({"kind": "validator_artifact", "path": str(artifact_path)})

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        key = (artifact["kind"], artifact["path"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
        if artifact["kind"] == "mission_contract":
            _record_artifact(store, task_key, "manifest", Path(artifact["path"]))
        elif artifact["kind"] == "executor_log":
            _record_artifact(store, task_key, "worker_log", Path(artifact["path"]))
        elif artifact["kind"] == "validator_log":
            _record_artifact(store, task_key, "review_log", Path(artifact["path"]))
        else:
            _record_artifact(store, task_key, "other", Path(artifact["path"]))
    return deduped


def _workspace_payload(result: WorkspacePreparationResult) -> dict[str, Any]:
    return json_safe(
        {
            "prepared": result.ok,
            "status": result.status,
            "summary": result.summary,
            "task_key": result.task_key,
            "repo_path": result.repo_path,
            "worktree_path": result.worktree_path,
            "branch": result.branch,
            "base_branch": result.base_branch,
            "base_sha": result.base_sha,
        }
    )


def _workspace_preview(request: ApprovedTaskRunRequest, task_key: str) -> dict[str, Any]:
    branch = f"task/{task_key}"
    worktree_root = request.worktree_root or (request.repo_path / ".worktrees")
    return json_safe(
        {
            "prepared": False,
            "status": RUN_STATUS_BLOCKED,
            "summary": "Worktree was not prepared.",
            "task_key": task_key,
            "repo_path": request.repo_path,
            "worktree_path": worktree_root / task_key,
            "branch": branch,
            "base_branch": request.base_branch,
            "base_sha": None,
        }
    )


def _executor_run_payload(
    run_id: str | None,
    executor: str,
    *,
    executor_result: ExecutorResult | None,
    started: bool,
    finished: bool,
    ok: bool,
    summary: str | None = None,
) -> dict[str, Any]:
    return json_safe(
        {
            "started": started,
            "finished": finished,
            "ok": ok,
            "run_id": run_id,
            "executor": executor_result.executor if executor_result is not None else executor,
            "status": executor_result.status if executor_result is not None else None,
            "summary": summary if summary is not None else (executor_result.summary if executor_result is not None else None),
            "log_path": executor_result.log_path if executor_result is not None else None,
        }
    )


def _preflight_payload(result: PreflightResult | None, *, ran: bool) -> dict[str, Any]:
    if result is None:
        return {
            "ran": False,
            "ok": None,
            "status": "skipped",
            "checks": [],
            "missing_required": [],
            "missing_optional": [],
            "recommended_commands": [],
        }
    data = result.to_dict()
    data["ran"] = ran
    return json_safe(data)


def _validator_payload(result: ValidatorResult) -> dict[str, Any]:
    return json_safe(
        {
            "name": result.validator,
            "ok": result.status == "passed",
            "status": result.status,
            "summary": result.summary,
            "log_path": result.log_path,
        }
    )


def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
