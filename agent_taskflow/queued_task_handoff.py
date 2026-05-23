"""Explicit queued-task handoff runner.

This module is the deterministic bridge between a Phase 6E Task
Execution Package and the explicit operator-driven approved task
runner. It verifies that a queued TaskRecord has a valid execution
package (implementation_prompt.md + task_execution_package.json) and,
under explicit --confirm-handoff, hands the task off to
approved_task_runner.run_approved_task.

This module is NOT a scheduler, NOT a background loop, NOT a webhook
handler, NOT a polling daemon, and does NOT auto-pick queued tasks.
Every invocation is one explicit operator command for one explicit
task key, gated by an explicit confirmation flag. It stops at the
runner's own final status (waiting_approval on success, blocked on
failure); it never continues into PR handoff, branch push, draft PR
creation, merge, approval, or cleanup.

Runtime preflight binding
-------------------------

In confirmed mode this module additionally REQUIRES an
``intake_runner_handoff`` artifact (produced by
``agent_taskflow.intake_runner_handoff``) and re-opens the verifier
report artifact bound to it. The handoff artifact's own claim that
the verifier passed is NOT trusted on its own; the queued handoff
re-validates the persisted verifier report at execution time. This
overlap between the verifier (verification time) and the queued
handoff (execution time) is intentional: it closes the TOCTOU gap
between the moment the verifier ran and the moment the runner is
asked to start. The handoff artifact itself is never treated as
execution permission, and confirmed mode never calls
``approved_task_runner`` unless every check in
:func:`_verify_intake_runner_handoff` passes.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent_taskflow.approved_task_runner import (
    APPROVED_TASK_STATUS,
    ApprovedTaskRunRequest,
    ApprovedTaskRunResult,
    ApprovedTaskRunnerError,
    run_approved_task,
)
from agent_taskflow.dispatcher import DEFAULT_VALIDATORS
from agent_taskflow.executors.base import Executor
from agent_taskflow.intake_runner_handoff import (
    SCHEMA_VERSION as INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
    STATUS_CREATED as INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
    VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
)
from agent_taskflow.models import TaskRecord, require_absolute_path, utc_now_iso
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.task_execution_package import (
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    SCHEMA_VERSION,
)
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator


DEFAULT_BASE_BRANCH = "main"
TASK_QUEUE_STATUS = "queued"
RUNNER_BLOCKED_STATUS = "blocked"
INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND = "queued_task_handoff"
VERIFIER_REPORT_STATUS_VALID = "valid"

# Phase C runtime audit boundary constants. These are intentionally
# disjoint from validator events (validation_result), executor events
# (executor_run_started / executor_run_finished), and the Phase A
# intake_runner_handoff_created event. Runtime audit evidence describes
# the runtime/queued handoff boundary itself: "why this runtime/queued
# handoff was allowed to start, when the runner was invoked, and what
# the runner returned." It is NOT action evidence and is NOT a second
# source of validator/approval truth.
RUNTIME_SOURCE = "queued_task_handoff_runtime"
RUNTIME_PREFLIGHT_EVENT_TYPE = "runtime_preflight_finished"
RUNTIME_EXECUTION_STARTED_EVENT_TYPE = "runtime_execution_started"
RUNTIME_EXECUTION_FINISHED_EVENT_TYPE = "runtime_execution_finished"
RUNTIME_EXECUTION_ARTIFACT_TYPE = "runtime_handoff_execution"
RUNTIME_EXECUTION_SCHEMA_VERSION = "runtime_handoff_execution.v1"
RUNTIME_EXECUTION_ARTIFACT_FILENAME = "runtime_handoff_execution.json"
RUNTIME_EXECUTION_DIRNAME = "runtime_handoff_executions"

_RUNTIME_EXECUTION_ID_TIMESTAMP = re.compile(r"[:\-Z]")


ApprovedTaskRunnerCallable = Callable[..., ApprovedTaskRunResult]


class QueuedTaskHandoffError(RuntimeError):
    """Raised when the queued-task handoff cannot proceed."""


def _normalize_validators(validators: Sequence[str] | None) -> tuple[str, ...]:
    if validators is None:
        return DEFAULT_VALIDATORS
    normalized = tuple(value.strip() for value in validators if str(value).strip())
    return normalized or DEFAULT_VALIDATORS


@dataclass(frozen=True)
class QueuedTaskHandoffRequest:
    """Input for one explicit queued-task handoff."""

    task_key: str
    executor: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    worktree_root: Path | None = None
    base_branch: str = DEFAULT_BASE_BRANCH
    validators: tuple[str, ...] = DEFAULT_VALIDATORS
    command: tuple[str, ...] | None = None
    preflight: bool = True
    dry_run: bool = True
    confirm_handoff: bool = False
    intake_runner_handoff_artifact_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        executor = self.executor.strip().lower()
        if not executor:
            raise ValueError("executor must not be empty")
        object.__setattr__(self, "executor", executor)

        object.__setattr__(
            self,
            "repo_path",
            require_absolute_path(self.repo_path, "repo_path"),
        )

        if self.db_path is None:
            db_path = default_db_path()
        else:
            db_path = require_absolute_path(self.db_path, "db_path")
        object.__setattr__(self, "db_path", Path(db_path))

        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                require_absolute_path(self.artifact_root, "artifact_root"),
            )

        if self.worktree_root is not None:
            object.__setattr__(
                self,
                "worktree_root",
                require_absolute_path(self.worktree_root, "worktree_root"),
            )

        base_branch = self.base_branch.strip()
        if not base_branch:
            raise ValueError("base_branch must not be empty")
        object.__setattr__(self, "base_branch", base_branch)

        object.__setattr__(
            self,
            "validators",
            _normalize_validators(self.validators),
        )

        if self.command is not None:
            command = tuple(part for part in self.command if str(part).strip())
            if not command:
                raise ValueError("command must not be empty when provided")
            object.__setattr__(self, "command", command)

        if self.dry_run and self.confirm_handoff:
            raise ValueError(
                "dry_run and confirm_handoff are mutually exclusive"
            )
        if not self.dry_run and not self.confirm_handoff:
            raise ValueError(
                "confirmed handoff requires confirm_handoff=True"
            )

        if self.intake_runner_handoff_artifact_path is not None:
            path = Path(self.intake_runner_handoff_artifact_path).expanduser()
            if not path.is_absolute():
                raise ValueError(
                    "intake_runner_handoff_artifact_path must be an absolute "
                    "path"
                )
            object.__setattr__(
                self, "intake_runner_handoff_artifact_path", path
            )
        elif self.confirm_handoff:
            # Confirmed queued handoff MUST be bound to an
            # intake_runner_handoff artifact so the runtime preflight
            # has a verifier report artifact to re-open. Dry-run is
            # permitted to omit the binding for previewing.
            raise ValueError(
                "confirmed queued task handoff requires "
                "intake_runner_handoff_artifact_path; dry-run may omit it"
            )


@dataclass(frozen=True)
class QueuedTaskHandoffResult:
    """Structured result for a queued-task handoff invocation."""

    ok: bool
    status: str
    phase: str
    task_key: str
    executor: str
    dry_run: bool
    package: dict[str, Any]
    handoff: dict[str, Any]
    runner_result: dict[str, Any] | None
    safety: dict[str, Any]
    error: str | None = None
    runtime: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "phase": self.phase,
            "task_key": self.task_key,
            "executor": self.executor,
            "dry_run": self.dry_run,
            "package": self.package,
            "handoff": self.handoff,
            "runner_result": self.runner_result,
            "safety": self.safety,
            "error": self.error,
            "runtime": self.runtime,
        }


def _safety_block(
    *,
    dry_run: bool,
    package_verified: bool,
    handoff_confirmed: bool,
    runner_started: bool,
    workspace_prepared: bool = False,
    executor_started: bool = False,
    validators_started: bool = False,
    db_written: bool = False,
    artifact_written: bool = False,
) -> dict[str, Any]:
    return {
        "read_only": dry_run and not runner_started,
        "db_written": db_written,
        "artifact_written": artifact_written,
        "package_verified": package_verified,
        "handoff_confirmed": handoff_confirmed,
        "approved_task_runner_started": runner_started,
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


def _blocked(
    request: QueuedTaskHandoffRequest,
    *,
    phase: str,
    error: str,
    package: dict[str, Any] | None = None,
    handoff_view: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> QueuedTaskHandoffResult:
    return QueuedTaskHandoffResult(
        ok=False,
        status="blocked",
        phase=phase,
        task_key=request.task_key,
        executor=request.executor,
        dry_run=request.dry_run,
        package=package or _empty_package_view(),
        handoff=_handoff_meta(
            request,
            handoff_view=handoff_view,
            approved_task_runner_invoked=False,
        ),
        runner_result=None,
        safety=_safety_block(
            dry_run=request.dry_run,
            package_verified=bool(package and package.get("verified")),
            handoff_confirmed=False,
            runner_started=False,
        ),
        error=error,
        runtime=runtime,
    )


def _empty_handoff_view() -> dict[str, Any]:
    """Return the default handoff-binding fields surfaced on every result.

    These fields make it explicit that confirmed execution always
    requires an intake_runner_handoff artifact and a re-validated
    persisted verifier report. When no handoff path is provided (or the
    handoff fails verification), ``intake_runner_handoff_verified`` is
    False and the remaining binding fields are ``None``.
    """

    return {
        "intake_runner_handoff_required_for_confirmed_execution": True,
        "intake_runner_handoff_artifact_path": None,
        "intake_runner_handoff_verified": False,
        "verifier_run_id": None,
        "verifier_report_path": None,
        "proposal_hash": None,
        "proposal_item_id": None,
        "item_hash": None,
        "confirmation_id": None,
        "confirmation_artifact_path": None,
        "expiration_still_valid": None,
    }


def _handoff_meta(
    request: QueuedTaskHandoffRequest,
    *,
    handoff_view: dict[str, Any] | None,
    approved_task_runner_invoked: bool,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "confirmed": bool(request.confirm_handoff),
        "approved_task_runner_invoked": approved_task_runner_invoked,
        "executor": request.executor,
        "base_branch": request.base_branch,
        "validators": list(request.validators),
        "command": list(request.command) if request.command else None,
        "preflight": request.preflight,
    }
    binding = _empty_handoff_view()
    if request.intake_runner_handoff_artifact_path is not None:
        binding["intake_runner_handoff_artifact_path"] = str(
            request.intake_runner_handoff_artifact_path
        )
    if handoff_view is not None:
        for key in binding:
            if key in handoff_view:
                binding[key] = handoff_view[key]
    meta.update(binding)
    return meta


def _empty_package_view() -> dict[str, Any]:
    return {
        "verified": False,
        "package_path": None,
        "implementation_prompt_path": None,
        "schema_version": None,
        "task_key": None,
        "status_before": None,
    }


def _verify_package(
    *,
    task: TaskRecord,
    request: QueuedTaskHandoffRequest,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify the on-disk Task Execution Package.

    Returns (package_view, error). Exactly one is non-None on the
    failure path; on success, error is None and package_view is the
    verified view dict.
    """

    artifact_dir = _resolve_artifact_dir(task, request)
    if artifact_dir is None:
        return None, (
            "Task has no artifact_dir and no artifact_root was supplied; "
            "cannot locate task_execution_package.json"
        )

    package_path = artifact_dir / PACKAGE_FILENAME
    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME

    view: dict[str, Any] = {
        "verified": False,
        "package_path": str(package_path),
        "implementation_prompt_path": str(prompt_path),
        "schema_version": None,
        "task_key": None,
        "status_before": None,
    }

    if not package_path.exists():
        return view, f"Task execution package is missing: {package_path}"

    if not prompt_path.exists():
        return view, f"Implementation prompt is missing: {prompt_path}"

    try:
        raw = package_path.read_text(encoding="utf-8")
    except OSError as exc:
        return view, f"Could not read task execution package: {exc}"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return view, f"Task execution package is not valid JSON: {exc}"

    if not isinstance(payload, dict):
        return view, "Task execution package must be a JSON object"

    schema_version = payload.get("schema_version")
    view["schema_version"] = schema_version
    if schema_version != SCHEMA_VERSION:
        return view, (
            f"Task execution package schema_version must be {SCHEMA_VERSION!r}, "
            f"got {schema_version!r}"
        )

    package_task_key = payload.get("task_key")
    view["task_key"] = package_task_key
    if package_task_key != task.task_key:
        return view, (
            f"Task execution package task_key {package_task_key!r} does not "
            f"match requested task_key {task.task_key!r}"
        )

    status_before = payload.get("status_before")
    view["status_before"] = status_before
    if status_before is not None and status_before != TASK_QUEUE_STATUS:
        return view, (
            f"Task execution package status_before must be {TASK_QUEUE_STATUS!r} "
            f"when present, got {status_before!r}"
        )

    package_prompt_path = payload.get("implementation_prompt_path")
    if package_prompt_path is not None and Path(package_prompt_path) != prompt_path:
        return view, (
            f"Task execution package implementation_prompt_path "
            f"{package_prompt_path!r} does not match expected {str(prompt_path)!r}"
        )

    package_self_path = payload.get("package_path")
    if package_self_path is not None and Path(package_self_path) != package_path:
        return view, (
            f"Task execution package package_path {package_self_path!r} "
            f"does not match expected {str(package_path)!r}"
        )

    view["verified"] = True
    return view, None


def _verify_intake_runner_handoff(
    *,
    request: QueuedTaskHandoffRequest,
    task: TaskRecord,
    package_view: dict[str, Any],
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify the intake_runner_handoff artifact and its bound verifier report.

    Returns ``(handoff_view, error)``. On success, ``error`` is None
    and ``handoff_view`` carries the persisted binding fields the
    queued handoff result surfaces. On failure, ``handoff_view``
    contains any binding fields that were successfully decoded before
    the failure was detected so the result remains diagnosable, and
    ``error`` is a human-readable description of the failed check.

    All checks must pass before the caller may invoke
    ``approved_task_runner`` in confirmed mode. The check set is
    intentionally a superset of the verifier's own checks because the
    verifier ran at verification time; this helper re-runs at
    execution time to close the TOCTOU gap between verification and
    runner invocation.
    """

    view = _empty_handoff_view()
    handoff_path = request.intake_runner_handoff_artifact_path
    assert handoff_path is not None
    view["intake_runner_handoff_artifact_path"] = str(handoff_path)

    if not handoff_path.exists():
        return view, (
            "intake_runner_handoff artifact does not exist: "
            f"{handoff_path}"
        )

    try:
        raw = handoff_path.read_text(encoding="utf-8")
    except OSError as exc:
        return view, (
            f"could not read intake_runner_handoff artifact: {exc}"
        )

    try:
        handoff = json.loads(raw)
    except json.JSONDecodeError as exc:
        return view, (
            "intake_runner_handoff artifact is not valid JSON: "
            f"{exc}"
        )

    if not isinstance(handoff, dict):
        return view, (
            "intake_runner_handoff artifact must be a JSON object"
        )

    schema_version = handoff.get("schema_version")
    if schema_version != INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION:
        return view, (
            "intake_runner_handoff artifact schema_version must be "
            f"{INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION!r}, got "
            f"{schema_version!r}"
        )

    if handoff.get("status") != INTAKE_RUNNER_HANDOFF_STATUS_CREATED:
        return view, (
            "intake_runner_handoff artifact status must be "
            f"{INTAKE_RUNNER_HANDOFF_STATUS_CREATED!r}, got "
            f"{handoff.get('status')!r}"
        )

    if handoff.get("mode") != "confirmed":
        return view, (
            "intake_runner_handoff artifact mode must be 'confirmed', "
            f"got {handoff.get('mode')!r}"
        )

    handoff_task_key = handoff.get("task_key")
    if handoff_task_key != request.task_key:
        return view, (
            "intake_runner_handoff artifact task_key "
            f"{handoff_task_key!r} does not match requested task_key "
            f"{request.task_key!r}"
        )

    if (
        handoff.get("recommended_command_kind")
        != INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
    ):
        return view, (
            "intake_runner_handoff artifact recommended_command_kind "
            "must be "
            f"{INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND!r}, got "
            f"{handoff.get('recommended_command_kind')!r}"
        )

    declared_artifact_path = handoff.get("artifact_path")
    if (
        declared_artifact_path is not None
        and Path(declared_artifact_path) != handoff_path
    ):
        return view, (
            "intake_runner_handoff artifact_path "
            f"{declared_artifact_path!r} does not match the file the "
            f"queued handoff was given: {str(handoff_path)!r}"
        )

    runner_contract = handoff.get("runner_contract")
    contract_error = _verify_runner_contract_flags(runner_contract)
    if contract_error is not None:
        return view, contract_error

    safety = handoff.get("safety")
    safety_error = _verify_handoff_safety_flags(safety)
    if safety_error is not None:
        return view, safety_error

    proposal = handoff.get("proposal") or {}
    confirmation = handoff.get("confirmation") or {}
    verifier_block = handoff.get("verifier_report") or {}

    proposal_hash = proposal.get("proposal_hash")
    proposal_item_id = proposal.get("proposal_item_id")
    item_hash = proposal.get("item_hash")
    confirmation_id = confirmation.get("confirmation_id")
    confirmation_artifact_path = confirmation.get(
        "confirmation_artifact_path"
    )
    verifier_run_id = verifier_block.get("verifier_run_id")
    verifier_report_path_raw = verifier_block.get("verifier_report_path")

    view["proposal_hash"] = proposal_hash
    view["proposal_item_id"] = proposal_item_id
    view["item_hash"] = item_hash
    view["confirmation_id"] = confirmation_id
    view["confirmation_artifact_path"] = confirmation_artifact_path
    view["verifier_run_id"] = verifier_run_id
    view["verifier_report_path"] = verifier_report_path_raw

    for label, value in (
        ("proposal.proposal_hash", proposal_hash),
        ("proposal.proposal_item_id", proposal_item_id),
        ("proposal.item_hash", item_hash),
        (
            "confirmation.confirmation_artifact_path",
            confirmation_artifact_path,
        ),
        ("verifier_report.verifier_run_id", verifier_run_id),
        ("verifier_report.verifier_report_path", verifier_report_path_raw),
    ):
        if not isinstance(value, str) or not value:
            return view, (
                f"intake_runner_handoff artifact {label} must be a "
                "non-empty string"
            )

    verifier_report_path = Path(verifier_report_path_raw)
    if not verifier_report_path.exists():
        return view, (
            "verifier report artifact does not exist: "
            f"{verifier_report_path}"
        )

    try:
        report_raw = verifier_report_path.read_text(encoding="utf-8")
    except OSError as exc:
        return view, (
            f"could not read verifier report artifact: {exc}"
        )

    try:
        report_artifact = json.loads(report_raw)
    except json.JSONDecodeError as exc:
        return view, (
            "verifier report artifact is not valid JSON: "
            f"{exc}"
        )

    if not isinstance(report_artifact, dict):
        return view, "verifier report artifact must be a JSON object"

    if (
        report_artifact.get("schema_version")
        != VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION
    ):
        return view, (
            "verifier report artifact schema_version must be "
            f"{VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION!r}, got "
            f"{report_artifact.get('schema_version')!r}"
        )

    if report_artifact.get("verifier_run_id") != verifier_run_id:
        return view, (
            "verifier report artifact verifier_run_id "
            f"{report_artifact.get('verifier_run_id')!r} does not match "
            f"handoff verifier_run_id {verifier_run_id!r}"
        )

    report = report_artifact.get("report")
    if not isinstance(report, dict):
        return view, (
            "verifier report artifact must contain a 'report' object"
        )

    if report.get("status") != VERIFIER_REPORT_STATUS_VALID:
        return view, (
            "verifier report status must be "
            f"{VERIFIER_REPORT_STATUS_VALID!r}, got "
            f"{report.get('status')!r}"
        )
    if report.get("verification_passed") is not True:
        return view, (
            "verifier report verification_passed must be True"
        )
    if report.get("eligible_for_command_specific_confirm") is not True:
        return view, (
            "verifier report eligible_for_command_specific_confirm "
            "must be True"
        )
    if report.get("execution_allowed") is not False:
        return view, (
            "verifier report execution_allowed must be False"
        )
    if report.get("execution_performed") is not False:
        return view, (
            "verifier report execution_performed must be False"
        )
    if report.get("action_evidence_created") is not False:
        return view, (
            "verifier report action_evidence_created must be False"
        )
    if report.get("task_key") != request.task_key:
        return view, (
            "verifier report task_key "
            f"{report.get('task_key')!r} does not match requested "
            f"task_key {request.task_key!r}"
        )
    if (
        report.get("recommended_command_kind")
        != INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
    ):
        return view, (
            "verifier report recommended_command_kind must be "
            f"{INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND!r}, got "
            f"{report.get('recommended_command_kind')!r}"
        )

    for label, handoff_value, report_value in (
        (
            "proposal_hash",
            proposal_hash,
            report.get("proposal_hash"),
        ),
        (
            "proposal_item_id",
            proposal_item_id,
            report.get("proposal_item_id"),
        ),
        ("item_hash", item_hash, report.get("item_hash")),
        (
            "confirmation_artifact_path",
            confirmation_artifact_path,
            report.get("confirmation_artifact_path"),
        ),
        (
            "confirmation_id",
            confirmation_id,
            report.get("confirmation_id"),
        ),
    ):
        if handoff_value != report_value:
            return view, (
                f"verifier report {label} {report_value!r} does not "
                f"match handoff {label} {handoff_value!r}"
            )

    expiration = report.get("expiration")
    expiration_ok, expiration_error = _handoff_expiration_still_valid(
        expiration, now=now
    )
    view["expiration_still_valid"] = expiration_ok
    if not expiration_ok:
        return view, (
            f"verifier report expiration is no longer valid: "
            f"{expiration_error}"
        )

    view["intake_runner_handoff_verified"] = True
    return view, None


def _verify_runner_contract_flags(
    runner_contract: Any,
) -> str | None:
    if not isinstance(runner_contract, dict):
        return (
            "intake_runner_handoff artifact runner_contract must be a "
            "JSON object"
        )
    expectations = {
        "requires_future_runtime_gate": True,
        "runner_may_start": False,
        "execution_allowed": False,
        "execution_performed": False,
        "executor_started": False,
        "validators_started": False,
        "action_evidence_created": False,
    }
    for key, expected in expectations.items():
        actual = runner_contract.get(key)
        if actual is not expected:
            return (
                "intake_runner_handoff artifact runner_contract."
                f"{key} must be {expected!r}, got {actual!r}"
            )
    return None


def _verify_handoff_safety_flags(safety: Any) -> str | None:
    if not isinstance(safety, dict):
        return (
            "intake_runner_handoff artifact safety must be a JSON object"
        )
    expectations = {
        "handoff_only": True,
        "will_execute": False,
        "will_start_background_worker": False,
        "will_mutate_github": False,
    }
    for key, expected in expectations.items():
        actual = safety.get(key)
        if actual is not expected:
            return (
                "intake_runner_handoff artifact safety."
                f"{key} must be {expected!r}, got {actual!r}"
            )
    return None


def _handoff_expiration_still_valid(
    expiration: Any,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Re-check the verifier-report TTL at execution time.

    Phase B intentionally does NOT trust the verifier report's own
    ``expired`` flag because that was computed at verification time.
    This helper recomputes the age from
    ``confirmation_created_at`` and the effective TTL and rejects any
    expiration block that is missing those fields or is now stale.
    """

    if not isinstance(expiration, dict):
        return False, "expiration block is missing or not an object"

    created_at_raw = expiration.get("confirmation_created_at")
    if not isinstance(created_at_raw, str) or not created_at_raw:
        return False, "expiration.confirmation_created_at is missing"

    effective = expiration.get("effective_max_age_minutes")
    if effective is None:
        effective = expiration.get("max_age_minutes")
    if not isinstance(effective, int) or effective < 0:
        return (
            False,
            "expiration.effective_max_age_minutes must be a non-negative "
            "integer",
        )

    try:
        created = _parse_iso8601_utc(created_at_raw)
    except ValueError as exc:
        return False, f"could not parse confirmation_created_at: {exc}"

    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - created).total_seconds()
    if age < 0:
        return False, "confirmation_created_at is in the future"
    max_age_seconds = effective * 60
    if age > max_age_seconds:
        return False, (
            f"confirmation is {age:.1f}s old at execution time; "
            f"max age is {max_age_seconds}s"
        )
    return True, None


def _parse_iso8601_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp the verifier produces.

    The verifier writes ``...Z`` suffixed timestamps. We normalize to
    ``+00:00`` so :func:`datetime.fromisoformat` accepts the input on
    older Python versions where ``Z`` parsing was added in 3.11.
    """

    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_artifact_dir(
    task: TaskRecord,
    request: QueuedTaskHandoffRequest,
) -> Path | None:
    if task.artifact_dir is not None:
        return task.artifact_dir
    if request.artifact_root is not None:
        return request.artifact_root / task.task_key
    return None


def _make_runtime_execution_id() -> str:
    timestamp = _RUNTIME_EXECUTION_ID_TIMESTAMP.sub("", utc_now_iso())
    return f"runtime-execution-{timestamp}-{secrets.token_hex(6)}"


def _runtime_artifact_path(
    artifact_dir: Path,
    runtime_execution_id: str,
) -> Path:
    return (
        artifact_dir
        / RUNTIME_EXECUTION_DIRNAME
        / runtime_execution_id
        / RUNTIME_EXECUTION_ARTIFACT_FILENAME
    )


def _runtime_safety_block() -> dict[str, Any]:
    """Return the safety flags embedded in every runtime audit record.

    These flags exist to make it impossible to mistake a runtime audit
    artifact or runtime audit event for action evidence, validator
    authority, scheduler loop output, or background-worker output. Every
    flag is fixed because Phase C explicitly does not add any of these
    behaviors; the runtime audit boundary is observation, not action.
    """

    return {
        "runtime_audit_only": True,
        "not_action_evidence": True,
        "not_validation_authority": True,
        "auto_selected_task": False,
        "batch_execution": False,
        "background_worker_started": False,
        "github_mutated_by_runtime": False,
        "approved": False,
        "rejected": False,
        "merged": False,
        "cleanup_performed": False,
    }


def _runtime_handoff_summary(
    request: QueuedTaskHandoffRequest,
    handoff_view: dict[str, Any] | None,
) -> dict[str, Any]:
    binding = _empty_handoff_view()
    if request.intake_runner_handoff_artifact_path is not None:
        binding["intake_runner_handoff_artifact_path"] = str(
            request.intake_runner_handoff_artifact_path
        )
    if handoff_view is not None:
        for key in binding:
            if key in handoff_view:
                binding[key] = handoff_view[key]
    return binding


def _build_runtime_artifact_payload(
    *,
    request: QueuedTaskHandoffRequest,
    handoff_view: dict[str, Any] | None,
    runtime_execution_id: str,
    created_at: str,
    preflight: dict[str, Any],
    approved_task_runner_block: dict[str, Any],
    runner_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the runtime_handoff_execution artifact payload.

    The artifact is runtime audit evidence: it records the runtime/queued
    handoff boundary decisions (preflight passed/failed, runner invoked
    or not, what the runner returned) so that a reader can audit *why*
    the queued handoff was allowed to start without needing to replay the
    DB events. The artifact intentionally summarizes - rather than
    duplicates - the runner's own validator records; validators remain
    authoritative via approved_task_runner / validation_result.
    """

    binding = _runtime_handoff_summary(request, handoff_view)
    return {
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "created_at": created_at,
        "source": RUNTIME_SOURCE,
        "task_key": request.task_key,
        "executor": request.executor,
        "dry_run": False,
        "intake_runner_handoff_artifact_path": binding[
            "intake_runner_handoff_artifact_path"
        ],
        "verifier_run_id": binding["verifier_run_id"],
        "verifier_report_path": binding["verifier_report_path"],
        "proposal_hash": binding["proposal_hash"],
        "proposal_item_id": binding["proposal_item_id"],
        "item_hash": binding["item_hash"],
        "confirmation_id": binding["confirmation_id"],
        "confirmation_artifact_path": binding["confirmation_artifact_path"],
        "expiration_still_valid": binding["expiration_still_valid"],
        "preflight": preflight,
        "approved_task_runner": approved_task_runner_block,
        "runner_result_summary": runner_summary,
        "safety": _runtime_safety_block(),
    }


def _write_runtime_artifact(
    *,
    artifact_dir: Path,
    runtime_execution_id: str,
    payload: dict[str, Any],
) -> Path:
    artifact_path = _runtime_artifact_path(artifact_dir, runtime_execution_id)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return artifact_path


def _runtime_preflight_event_payload(
    *,
    request: QueuedTaskHandoffRequest,
    handoff_view: dict[str, Any] | None,
    runtime_execution_id: str,
    preflight_passed: bool,
    package_verified: bool,
    intake_runner_handoff_verified: bool,
    error: str | None,
) -> dict[str, Any]:
    binding = _runtime_handoff_summary(request, handoff_view)
    return {
        "kind": RUNTIME_PREFLIGHT_EVENT_TYPE,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "task_key": request.task_key,
        "executor": request.executor,
        "preflight_passed": preflight_passed,
        "package_verified": package_verified,
        "intake_runner_handoff_verified": intake_runner_handoff_verified,
        "expiration_still_valid": binding["expiration_still_valid"],
        "intake_runner_handoff_artifact_path": binding[
            "intake_runner_handoff_artifact_path"
        ],
        "verifier_run_id": binding["verifier_run_id"],
        "verifier_report_path": binding["verifier_report_path"],
        "proposal_hash": binding["proposal_hash"],
        "proposal_item_id": binding["proposal_item_id"],
        "item_hash": binding["item_hash"],
        "confirmation_id": binding["confirmation_id"],
        "error": error,
        "approved_task_runner_invoked": False,
        "executor_started": False,
        "validators_started": False,
        "action_evidence_created": False,
        "not_action_evidence": True,
    }


def _runtime_execution_started_event_payload(
    *,
    request: QueuedTaskHandoffRequest,
    handoff_view: dict[str, Any] | None,
    runtime_execution_id: str,
) -> dict[str, Any]:
    binding = _runtime_handoff_summary(request, handoff_view)
    return {
        "kind": RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "task_key": request.task_key,
        "executor": request.executor,
        "approved_task_runner_invoked": True,
        "intake_runner_handoff_artifact_path": binding[
            "intake_runner_handoff_artifact_path"
        ],
        "verifier_run_id": binding["verifier_run_id"],
        "verifier_report_path": binding["verifier_report_path"],
        "proposal_hash": binding["proposal_hash"],
        "proposal_item_id": binding["proposal_item_id"],
        "item_hash": binding["item_hash"],
        "confirmation_id": binding["confirmation_id"],
        "not_action_evidence": True,
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _runtime_execution_finished_event_payload(
    *,
    request: QueuedTaskHandoffRequest,
    handoff_view: dict[str, Any] | None,
    runtime_execution_id: str,
    runner_returned: bool,
    runner_ok: bool,
    runner_status: str | None,
    runner_phase: str | None,
    final_status: str,
    runner_error: str | None,
    workspace_prepared: bool | None,
    executor_started: bool | None,
    validators_started: bool | None,
    db_written_by_runner: bool | None,
    artifact_written_by_runner: bool | None,
) -> dict[str, Any]:
    binding = _runtime_handoff_summary(request, handoff_view)
    return {
        "kind": RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "task_key": request.task_key,
        "executor": request.executor,
        "runner_returned": runner_returned,
        "runner_ok": runner_ok,
        "runner_status": runner_status,
        "runner_phase": runner_phase,
        "final_status": final_status,
        "runner_error": runner_error,
        "workspace_prepared": workspace_prepared,
        "executor_started": executor_started,
        "validators_started": validators_started,
        "db_written_by_runner": db_written_by_runner,
        "artifact_written_by_runner": artifact_written_by_runner,
        "intake_runner_handoff_artifact_path": binding[
            "intake_runner_handoff_artifact_path"
        ],
        "verifier_run_id": binding["verifier_run_id"],
        "verifier_report_path": binding["verifier_report_path"],
        "not_validation_authority": True,
        "not_action_evidence": True,
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _runtime_reference_block(
    *,
    runtime_execution_id: str,
    runtime_artifact_path: Path | None,
    preflight_event_recorded: bool,
    execution_started_event_recorded: bool,
    execution_finished_event_recorded: bool,
) -> dict[str, Any]:
    return {
        "runtime_execution_id": runtime_execution_id,
        "runtime_execution_artifact_path": (
            str(runtime_artifact_path) if runtime_artifact_path else None
        ),
        "runtime_preflight_event_recorded": preflight_event_recorded,
        "runtime_execution_started_event_recorded": (
            execution_started_event_recorded
        ),
        "runtime_execution_finished_event_recorded": (
            execution_finished_event_recorded
        ),
        "runtime_audit_only": True,
        "not_action_evidence": True,
    }


def run_queued_task_handoff(
    request: QueuedTaskHandoffRequest,
    *,
    store: TaskMirrorStore | None = None,
    approved_task_runner: ApprovedTaskRunnerCallable = run_approved_task,
    executor_registry: Mapping[str, Executor] | None = None,
    validator_registry: Mapping[str, Validator] | None = None,
    preflight_runner=None,
) -> QueuedTaskHandoffResult:
    """Verify the execution package and, on confirm, hand off to the runner.

    The approved_task_runner callable is injectable so tests can verify
    handoff behavior without running real executors, validators, or git
    worktree commands.
    """

    current_store = store or TaskMirrorStore(request.db_path)

    task = current_store.get_task(request.task_key)
    if task is None:
        return _blocked(
            request,
            phase="selection",
            error=f"Task not found: {request.task_key}",
        )

    if task.status != TASK_QUEUE_STATUS:
        return _blocked(
            request,
            phase="selection",
            error=(
                f"Queued-task handoff requires task.status={TASK_QUEUE_STATUS!r}; "
                f"current status: {task.status!r}"
            ),
        )

    package_view, package_error = _verify_package(task=task, request=request)
    if package_error is not None:
        return _blocked(
            request,
            phase="package_verification",
            error=package_error,
            package=package_view,
        )

    assert package_view is not None
    assert package_view["verified"] is True

    handoff_view: dict[str, Any] | None = None
    handoff_error: str | None = None
    if request.intake_runner_handoff_artifact_path is not None:
        handoff_view, handoff_error = _verify_intake_runner_handoff(
            request=request,
            task=task,
            package_view=package_view,
        )

    if request.dry_run:
        # Dry-run is preview-only. Phase C explicitly does NOT write any
        # runtime events or runtime audit artifact in dry-run because
        # the runtime boundary has not been crossed: approved_task_runner
        # is never invoked. The handoff_verification short-circuit below
        # mirrors confirmed mode so previews still surface why confirmed
        # execution would be blocked.
        if handoff_error is not None:
            return _blocked(
                request,
                phase="handoff_verification",
                error=handoff_error,
                package=package_view,
                handoff_view=handoff_view,
            )
        return QueuedTaskHandoffResult(
            ok=True,
            status="preview",
            phase="preview",
            task_key=request.task_key,
            executor=request.executor,
            dry_run=True,
            package=package_view,
            handoff=_handoff_meta(
                request,
                handoff_view=handoff_view,
                approved_task_runner_invoked=False,
            ),
            runner_result=None,
            safety=_safety_block(
                dry_run=True,
                package_verified=True,
                handoff_confirmed=False,
                runner_started=False,
            ),
            error=None,
            runtime=None,
        )

    # Confirmed mode below. __post_init__ already guaranteed an
    # absolute intake_runner_handoff_artifact_path was supplied. From
    # here on we record runtime audit evidence at every runtime
    # boundary: preflight outcome, runner invocation, runner return /
    # exception. The runtime artifact + events are NOT action evidence
    # and they do NOT replace validator authority - the validator
    # authority remains approved_task_runner / validation_result.
    assert request.intake_runner_handoff_artifact_path is not None
    artifact_dir = _resolve_artifact_dir(task, request)
    if artifact_dir is None:
        # Confirmed mode without a resolvable artifact_dir is not
        # supportable: we cannot persist runtime audit evidence, so
        # block before invoking the runner. This is defensive; the
        # CLI path always supplies artifact_root and task.artifact_dir
        # is set by ingestion.
        return _blocked(
            request,
            phase="handoff_verification",
            error=(
                "Confirmed queued handoff requires a resolvable "
                "artifact_dir; supply --artifact-root or ensure "
                "task.artifact_dir is set"
            ),
            package=package_view,
            handoff_view=handoff_view,
        )

    runtime_execution_id = _make_runtime_execution_id()
    created_at = utc_now_iso()

    intake_runner_handoff_verified = bool(
        handoff_view is not None
        and handoff_view.get("intake_runner_handoff_verified")
    )
    expiration_still_valid = (
        handoff_view.get("expiration_still_valid")
        if handoff_view is not None
        else None
    )
    preflight_passed = handoff_error is None and intake_runner_handoff_verified

    # 1. Record runtime_preflight_finished event.
    preflight_payload = _runtime_preflight_event_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
        preflight_passed=preflight_passed,
        package_verified=True,
        intake_runner_handoff_verified=intake_runner_handoff_verified,
        error=handoff_error,
    )
    current_store.record_task_event(
        request.task_key,
        RUNTIME_PREFLIGHT_EVENT_TYPE,
        RUNTIME_SOURCE,
        message=(
            f"Runtime preflight {'passed' if preflight_passed else 'failed'} "
            f"for {request.task_key} (runtime_execution_id={runtime_execution_id})"
        ),
        payload=preflight_payload,
    )

    if not preflight_passed:
        # Handoff verification failed. Persist the runtime audit
        # artifact so the operator has a single readable record of
        # why the runner was not invoked, and return blocked. The
        # runner is NOT called; approved_task_runner.invoked is False.
        runtime_artifact_payload = _build_runtime_artifact_payload(
            request=request,
            handoff_view=handoff_view,
            runtime_execution_id=runtime_execution_id,
            created_at=created_at,
            preflight={
                "passed": False,
                "package_verified": True,
                "intake_runner_handoff_verified": (
                    intake_runner_handoff_verified
                ),
                "expiration_still_valid": expiration_still_valid,
                "error": handoff_error,
            },
            approved_task_runner_block={
                "invoked": False,
                "ok": None,
                "status": None,
                "phase": None,
                "executor_started": False,
                "validators_started": False,
            },
            runner_summary=None,
        )
        runtime_artifact_path = _write_runtime_artifact(
            artifact_dir=artifact_dir,
            runtime_execution_id=runtime_execution_id,
            payload=runtime_artifact_payload,
        )
        current_store.record_task_artifact(
            request.task_key,
            RUNTIME_EXECUTION_ARTIFACT_TYPE,
            runtime_artifact_path,
        )
        runtime_block = _runtime_reference_block(
            runtime_execution_id=runtime_execution_id,
            runtime_artifact_path=runtime_artifact_path,
            preflight_event_recorded=True,
            execution_started_event_recorded=False,
            execution_finished_event_recorded=False,
        )
        return _blocked(
            request,
            phase="handoff_verification",
            error=handoff_error or "intake_runner_handoff preflight failed",
            package=package_view,
            handoff_view=handoff_view,
            runtime=runtime_block,
        )

    assert handoff_view is not None

    # 2. Record runtime_execution_started event immediately before
    #    calling approved_task_runner. This event records ONLY that
    #    runtime preflight passed and the runner invocation is about
    #    to begin; it does not assert anything about executor or
    #    validator success.
    execution_started_payload = _runtime_execution_started_event_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
    )
    current_store.record_task_event(
        request.task_key,
        RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
        RUNTIME_SOURCE,
        message=(
            f"Runtime preflight passed for {request.task_key}; "
            f"invoking approved_task_runner "
            f"(runtime_execution_id={runtime_execution_id})"
        ),
        payload=execution_started_payload,
    )

    runner_request = ApprovedTaskRunRequest(
        task_key=request.task_key,
        executor=request.executor,
        repo_path=request.repo_path,
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        worktree_root=request.worktree_root,
        base_branch=request.base_branch,
        validators=request.validators,
        confirm_approved_task=True,
        dry_run=False,
        preflight=request.preflight,
        command=request.command,
    )

    runner_kwargs: dict[str, Any] = {"store": current_store}
    if executor_registry is not None:
        runner_kwargs["executor_registry"] = executor_registry
    if validator_registry is not None:
        runner_kwargs["validator_registry"] = validator_registry
    if preflight_runner is not None:
        runner_kwargs["preflight_runner"] = preflight_runner

    runner_exception: ApprovedTaskRunnerError | None = None
    runner_dict: dict[str, Any] | None = None
    try:
        runner_result = approved_task_runner(runner_request, **runner_kwargs)
    except ApprovedTaskRunnerError as exc:
        runner_exception = exc
    else:
        runner_dict = _runner_result_to_dict(runner_result)

    if runner_exception is not None:
        # Runner raised before producing a structured result. Record
        # the runtime audit terminus and return blocked.
        finished_payload = _runtime_execution_finished_event_payload(
            request=request,
            handoff_view=handoff_view,
            runtime_execution_id=runtime_execution_id,
            runner_returned=False,
            runner_ok=False,
            runner_status=None,
            runner_phase=None,
            final_status="blocked",
            runner_error=str(runner_exception),
            workspace_prepared=None,
            executor_started=None,
            validators_started=None,
            db_written_by_runner=None,
            artifact_written_by_runner=None,
        )
        current_store.record_task_event(
            request.task_key,
            RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
            RUNTIME_SOURCE,
            message=(
                f"approved_task_runner raised for {request.task_key} "
                f"(runtime_execution_id={runtime_execution_id})"
            ),
            payload=finished_payload,
        )
        runtime_artifact_payload = _build_runtime_artifact_payload(
            request=request,
            handoff_view=handoff_view,
            runtime_execution_id=runtime_execution_id,
            created_at=created_at,
            preflight={
                "passed": True,
                "package_verified": True,
                "intake_runner_handoff_verified": True,
                "expiration_still_valid": expiration_still_valid,
                "error": None,
            },
            approved_task_runner_block={
                "invoked": True,
                "ok": False,
                "status": None,
                "phase": None,
                "executor_started": None,
                "validators_started": None,
            },
            runner_summary={
                "ok": False,
                "status": None,
                "phase": None,
                "error": str(runner_exception),
                "returned": False,
            },
        )
        runtime_artifact_path = _write_runtime_artifact(
            artifact_dir=artifact_dir,
            runtime_execution_id=runtime_execution_id,
            payload=runtime_artifact_payload,
        )
        current_store.record_task_artifact(
            request.task_key,
            RUNTIME_EXECUTION_ARTIFACT_TYPE,
            runtime_artifact_path,
        )
        runtime_block = _runtime_reference_block(
            runtime_execution_id=runtime_execution_id,
            runtime_artifact_path=runtime_artifact_path,
            preflight_event_recorded=True,
            execution_started_event_recorded=True,
            execution_finished_event_recorded=True,
        )
        return QueuedTaskHandoffResult(
            ok=False,
            status="blocked",
            phase="runner",
            task_key=request.task_key,
            executor=request.executor,
            dry_run=False,
            package=package_view,
            handoff=_handoff_meta(
                request,
                handoff_view=handoff_view,
                approved_task_runner_invoked=True,
            ),
            runner_result=None,
            safety=_safety_block(
                dry_run=False,
                package_verified=True,
                handoff_confirmed=True,
                runner_started=True,
            ),
            error=str(runner_exception),
            runtime=runtime_block,
        )

    assert runner_dict is not None
    runner_safety = runner_dict.get("safety") or {}
    runner_status = runner_dict.get("status")
    runner_phase = runner_dict.get("phase")
    runner_ok_flag = bool(runner_dict.get("ok"))
    ok = runner_ok_flag and runner_status == APPROVED_TASK_STATUS
    status = APPROVED_TASK_STATUS if ok else "blocked"
    phase = APPROVED_TASK_STATUS if ok else "runner"

    workspace_prepared = bool(runner_safety.get("workspace_prepared"))
    executor_started = bool(runner_safety.get("executor_started"))
    validators_started = bool(runner_safety.get("validators_started"))
    db_written_by_runner = bool(runner_safety.get("db_written"))
    artifact_written_by_runner = bool(runner_safety.get("artifact_written"))

    finished_payload = _runtime_execution_finished_event_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
        runner_returned=True,
        runner_ok=runner_ok_flag,
        runner_status=runner_status if isinstance(runner_status, str) else None,
        runner_phase=runner_phase if isinstance(runner_phase, str) else None,
        final_status=status,
        runner_error=runner_dict.get("error"),
        workspace_prepared=workspace_prepared,
        executor_started=executor_started,
        validators_started=validators_started,
        db_written_by_runner=db_written_by_runner,
        artifact_written_by_runner=artifact_written_by_runner,
    )
    current_store.record_task_event(
        request.task_key,
        RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
        RUNTIME_SOURCE,
        message=(
            f"approved_task_runner returned status={runner_status!r} for "
            f"{request.task_key} (runtime_execution_id={runtime_execution_id})"
        ),
        payload=finished_payload,
    )

    runtime_artifact_payload = _build_runtime_artifact_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
        created_at=created_at,
        preflight={
            "passed": True,
            "package_verified": True,
            "intake_runner_handoff_verified": True,
            "expiration_still_valid": expiration_still_valid,
            "error": None,
        },
        approved_task_runner_block={
            "invoked": True,
            "ok": runner_ok_flag,
            "status": runner_status if isinstance(runner_status, str) else None,
            "phase": runner_phase if isinstance(runner_phase, str) else None,
            "executor_started": executor_started,
            "validators_started": validators_started,
        },
        runner_summary={
            "ok": runner_ok_flag,
            "status": runner_status if isinstance(runner_status, str) else None,
            "phase": runner_phase if isinstance(runner_phase, str) else None,
            "error": runner_dict.get("error"),
            "returned": True,
        },
    )
    runtime_artifact_path = _write_runtime_artifact(
        artifact_dir=artifact_dir,
        runtime_execution_id=runtime_execution_id,
        payload=runtime_artifact_payload,
    )
    current_store.record_task_artifact(
        request.task_key,
        RUNTIME_EXECUTION_ARTIFACT_TYPE,
        runtime_artifact_path,
    )
    runtime_block = _runtime_reference_block(
        runtime_execution_id=runtime_execution_id,
        runtime_artifact_path=runtime_artifact_path,
        preflight_event_recorded=True,
        execution_started_event_recorded=True,
        execution_finished_event_recorded=True,
    )

    return QueuedTaskHandoffResult(
        ok=ok,
        status=status,
        phase=phase,
        task_key=request.task_key,
        executor=request.executor,
        dry_run=False,
        package=package_view,
        handoff=_handoff_meta(
            request,
            handoff_view=handoff_view,
            approved_task_runner_invoked=True,
        ),
        runner_result=runner_dict,
        safety=_safety_block(
            dry_run=False,
            package_verified=True,
            handoff_confirmed=True,
            runner_started=True,
            workspace_prepared=workspace_prepared,
            executor_started=executor_started,
            validators_started=validators_started,
            db_written=db_written_by_runner,
            artifact_written=artifact_written_by_runner,
        ),
        error=runner_dict.get("error") if not ok else None,
        runtime=runtime_block,
    )


def _runner_result_to_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif isinstance(result, dict):
        payload = result
    else:  # pragma: no cover - defensive shape check
        raise TypeError(
            "approved_task_runner must return an ApprovedTaskRunResult or dict"
        )
    if not isinstance(payload, dict):  # pragma: no cover - defensive shape check
        raise TypeError("approved_task_runner result.to_dict() must return a dict")
    return payload


__all__ = [
    "APPROVED_TASK_STATUS",
    "DEFAULT_BASE_BRANCH",
    "RUNTIME_EXECUTION_ARTIFACT_TYPE",
    "RUNTIME_EXECUTION_FINISHED_EVENT_TYPE",
    "RUNTIME_EXECUTION_SCHEMA_VERSION",
    "RUNTIME_EXECUTION_STARTED_EVENT_TYPE",
    "RUNTIME_PREFLIGHT_EVENT_TYPE",
    "RUNTIME_SOURCE",
    "QueuedTaskHandoffError",
    "QueuedTaskHandoffRequest",
    "QueuedTaskHandoffResult",
    "run_queued_task_handoff",
]
