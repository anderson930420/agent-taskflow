"""Level 6A minimal runtime preflight + approved_task_runner path.

This module is the explicit operator-gated bridge from a Level 5A
``intake_runner_handoff`` (produced by
``agent_taskflow.intake_runner_handoff_from_verifier_report``) to a
one-shot invocation of ``approved_task_runner``. The binding helper is
read-only and the execution helper is dry-run by default.

A ``runtime_handoff_execution`` artifact/event recorded here is runtime
audit evidence only. It is not approval, not merge, not cleanup, not a
scheduler loop, not a background worker, and does not auto-pick tasks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.intake_runner_handoff_from_verifier_report import (
    HANDOFF_ARTIFACT_TYPE,
    HANDOFF_EVENT_TYPE,
)
from agent_taskflow.models import utc_now_iso
from agent_taskflow.scheduler_proposals import verify_proposal_hashes
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


RUNTIME_EXECUTION_SCHEMA_VERSION = "runtime_handoff_execution_from_handoff.v1"
RUNTIME_EXECUTION_SOURCE = "runtime_handoff_execution_from_handoff"
RUNTIME_EXECUTION_ARTIFACT_TYPE = "runtime_handoff_execution"
RUNTIME_PREFLIGHT_EVENT_TYPE = "runtime_preflight_finished"
RUNTIME_STARTED_EVENT_TYPE = "runtime_execution_started"
RUNTIME_FINISHED_EVENT_TYPE = "runtime_execution_finished"
HANDOFF_CONSUMED_EVENT_TYPE = "intake_runner_handoff_consumed"

RUNTIME_EXECUTION_SAFETY_FLAGS: dict[str, bool] = {
    "runtime_started": False,
    "approved_task_runner_called": False,
    "executor_started": False,
    "validators_started": False,
    "github_mutated": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "background_worker_started": False,
    "scheduler_loop_started": False,
    "automatic_task_picking_started": False,
    "requires_human_review_after_runtime": True,
    "not_approval": True,
    "not_merge": True,
    "not_cleanup": True,
}


REASON_TASK_MISSING = "task_missing"
REASON_HANDOFF_NOT_FOUND = "handoff_not_found"
REASON_HANDOFF_AMBIGUOUS = "handoff_ambiguous"
REASON_HANDOFF_ARTIFACT_PATH_MISSING = "handoff_artifact_path_missing"
REASON_HANDOFF_ARTIFACT_FILE_MISSING = "handoff_artifact_file_missing"
REASON_HANDOFF_ARTIFACT_JSON_MALFORMED = "handoff_artifact_json_malformed"
REASON_HANDOFF_ARTIFACT_JSON_NOT_OBJECT = "handoff_artifact_json_not_object"
REASON_HANDOFF_SAFETY_FLAGS_INVALID = "handoff_safety_flags_invalid"
REASON_HANDOFF_BINDING_MISMATCH = "handoff_binding_mismatch"
REASON_VERIFIER_REPORT_ARTIFACT_PATH_MISSING = (
    "verifier_report_artifact_path_missing"
)
REASON_VERIFIER_REPORT_ARTIFACT_FILE_MISSING = (
    "verifier_report_artifact_file_missing"
)
REASON_VERIFIER_REPORT_ARTIFACT_JSON_MALFORMED = (
    "verifier_report_artifact_json_malformed"
)
REASON_VERIFIER_REPORT_ARTIFACT_JSON_NOT_OBJECT = (
    "verifier_report_artifact_json_not_object"
)
REASON_VERIFIER_REPORT_FAILED = "verifier_report_failed"
REASON_CONFIRMATION_ARTIFACT_FILE_MISSING = "confirmation_artifact_file_missing"
REASON_CONFIRMATION_BINDING_MISMATCH = "confirmation_binding_mismatch"
REASON_PROPOSAL_ARTIFACT_FILE_MISSING = "proposal_artifact_file_missing"
REASON_PROPOSAL_HASH_MISMATCH = "proposal_hash_mismatch"
REASON_PROPOSAL_ITEM_ID_MISSING_FROM_ARTIFACT = (
    "proposal_item_id_missing_from_artifact"
)
REASON_ITEM_HASH_MISMATCH = "item_hash_mismatch"
REASON_TASK_STATUS_MISMATCH = "task_status_mismatch"
REASON_RECOMMENDED_COMMAND_KIND_MISMATCH = "recommended_command_kind_mismatch"
REASON_DUPLICATE_RUNTIME_EXECUTION = "duplicate_runtime_execution"
REASON_HANDOFF_ALREADY_CONSUMED = "intake_runner_handoff_already_consumed"


_REQUIRED_CHECKS: tuple[str, ...] = (
    "handoff_exists",
    "handoff_artifact_exists",
    "handoff_safety_flags_valid",
    "handoff_binding_matches",
    "verifier_report_artifact_exists",
    "verifier_report_passed",
    "confirmation_artifact_exists",
    "proposal_artifact_exists",
    "proposal_hash_matches_artifact",
    "proposal_item_id_exists",
    "item_hash_matches_selected_item",
    "task_still_exists",
    "task_status_matches_expected",
    "recommended_command_kind_matches",
    "duplicate_runtime_execution_absent",
    "intake_runner_handoff_not_consumed",
)


_HANDOFF_REQUIRED_BINDING_KEYS: tuple[str, ...] = (
    "verifier_report_id",
    "confirmation_id",
    "proposal_hash",
    "proposal_item_id",
    "item_hash",
    "recommended_command_kind",
    "verifier_report_artifact_path",
    "confirmation_artifact_path",
    "proposal_artifact_path",
)


class RuntimeHandoffExecutionError(RuntimeError):
    """Raised when runtime execution cannot proceed safely."""


@dataclass(frozen=True)
class RuntimeHandoffExecutionRequest:
    """Inputs to the Level 6A runtime preflight and execution helpers."""

    db_path: Path
    artifact_root: Path
    task_key: str
    handoff_id: str
    verifier_report_id: str | None = None
    confirmation_id: str | None = None
    proposal_hash: str | None = None
    proposal_item_id: str | None = None
    item_hash: str | None = None
    recommended_command_kind: str | None = None
    handoff_artifact_path: Path | None = None
    dry_run: bool = True
    confirm_run_approved_task_runner: bool = False
    operator: str | None = None
    operator_note: str | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        artifact_root = Path(self.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        object.__setattr__(self, "artifact_root", artifact_root)

        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        handoff_id = (self.handoff_id or "").strip()
        if not handoff_id:
            raise ValueError("handoff_id must not be empty")
        object.__setattr__(self, "handoff_id", handoff_id)

        for field_name in (
            "verifier_report_id",
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "operator",
            "operator_note",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = value.strip()
            object.__setattr__(self, field_name, stripped or None)

        if self.handoff_artifact_path is not None:
            object.__setattr__(
                self,
                "handoff_artifact_path",
                Path(self.handoff_artifact_path).expanduser(),
            )


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def check_runtime_handoff_preflight(
    request: RuntimeHandoffExecutionRequest,
) -> dict[str, Any]:
    """Return a read-only Level 6A runtime preflight report."""

    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {name: False for name in _REQUIRED_CHECKS}

    handoff_view: dict[str, Any] = {
        "handoff_id": request.handoff_id,
        "verifier_report_id": None,
        "confirmation_id": None,
        "proposal_hash": None,
        "proposal_item_id": None,
        "item_hash": None,
        "recommended_command_kind": None,
        "handoff_artifact_path": None,
        "verifier_report_artifact_path": None,
        "confirmation_artifact_path": None,
        "proposal_artifact_path": None,
    }
    current_view: dict[str, Any] = {
        "task_exists": False,
        "task_status": None,
        "expected_status": None,
        "duplicate_runtime_execution_count": 0,
        "intake_runner_handoff_consumed_count": 0,
    }

    task_record = None
    store: TaskMirrorStore | None = None
    items: list[dict[str, Any]] = []
    if request.db_path.exists():
        store = TaskMirrorStore(request.db_path)
        task_record = store.get_task(request.task_key)
        if task_record is not None:
            checks["task_still_exists"] = True
            current_view["task_exists"] = True
            current_view["task_status"] = task_record.status
            items = _read_task_handoff_items(store, request.task_key)
        else:
            reasons.append(REASON_TASK_MISSING)
    else:
        reasons.append(REASON_TASK_MISSING)

    matches = _filter_handoff_items(items, request)
    handoff_payload: dict[str, Any] | None = None
    verifier_payload: dict[str, Any] | None = None
    confirmation_payload: dict[str, Any] | None = None
    proposal_payload: dict[str, Any] | None = None
    proposal_item: dict[str, Any] | None = None

    if not matches:
        reasons.append(REASON_HANDOFF_NOT_FOUND)
    elif len(matches) > 1:
        reasons.append(REASON_HANDOFF_AMBIGUOUS)
    else:
        selected = matches[0]
        checks["handoff_exists"] = True
        warnings.extend(selected.get("readback_warnings") or [])
        _copy_selected_handoff(selected, handoff_view)

        handoff_payload, handoff_reasons = _load_json_object(
            selected.get("artifact_path"),
            path_missing_reason=REASON_HANDOFF_ARTIFACT_PATH_MISSING,
            file_missing_reason=REASON_HANDOFF_ARTIFACT_FILE_MISSING,
            json_malformed_reason=REASON_HANDOFF_ARTIFACT_JSON_MALFORMED,
            json_not_object_reason=REASON_HANDOFF_ARTIFACT_JSON_NOT_OBJECT,
        )
        reasons.extend(handoff_reasons)

        if handoff_payload is not None:
            checks["handoff_artifact_exists"] = True
            _populate_handoff_view_from_payload(handoff_payload, handoff_view)
            _verify_handoff_safety_flags(
                payload=handoff_payload, checks=checks, reasons=reasons
            )
            _verify_handoff_binding(
                payload=handoff_payload,
                selected=selected,
                request=request,
                checks=checks,
                reasons=reasons,
            )

            verifier_payload, verifier_reasons = _load_json_object(
                handoff_payload.get("verifier_report_artifact_path"),
                path_missing_reason=REASON_VERIFIER_REPORT_ARTIFACT_PATH_MISSING,
                file_missing_reason=REASON_VERIFIER_REPORT_ARTIFACT_FILE_MISSING,
                json_malformed_reason=(
                    REASON_VERIFIER_REPORT_ARTIFACT_JSON_MALFORMED
                ),
                json_not_object_reason=(
                    REASON_VERIFIER_REPORT_ARTIFACT_JSON_NOT_OBJECT
                ),
            )
            reasons.extend(verifier_reasons)
            if verifier_payload is not None:
                checks["verifier_report_artifact_exists"] = True
                if verifier_payload.get("verification_passed") is True:
                    checks["verifier_report_passed"] = True
                else:
                    reasons.append(REASON_VERIFIER_REPORT_FAILED)

                confirmation_payload, _confirmation_reasons = _read_json_payload(
                    Path(str(handoff_payload.get("confirmation_artifact_path") or ""))
                )
                if confirmation_payload is None:
                    reasons.append(REASON_CONFIRMATION_ARTIFACT_FILE_MISSING)
                else:
                    checks["confirmation_artifact_exists"] = True
                    _verify_confirmation_payload(
                        payload=confirmation_payload,
                        verifier=verifier_payload,
                        checks=checks,
                        reasons=reasons,
                    )

                proposal_payload, _proposal_reasons = _read_json_payload(
                    Path(str(handoff_payload.get("proposal_artifact_path") or ""))
                )
                if proposal_payload is None:
                    reasons.append(REASON_PROPOSAL_ARTIFACT_FILE_MISSING)
                else:
                    checks["proposal_artifact_exists"] = True
                    hash_report = verify_proposal_hashes(proposal_payload)
                    proposal_item = _find_proposal_item(
                        proposal_payload,
                        _string_value(verifier_payload.get("proposal_item_id")),
                    )
                    _verify_proposal_payload(
                        verifier=verifier_payload,
                        proposal_item=proposal_item,
                        hash_report=hash_report,
                        checks=checks,
                        reasons=reasons,
                    )

                _verify_current_task_and_kind(
                    verifier=verifier_payload,
                    confirmation_payload=confirmation_payload,
                    proposal_item=proposal_item,
                    task_record=task_record,
                    current_view=current_view,
                    checks=checks,
                    reasons=reasons,
                )

                if verifier_payload.get("verifier_run_id") is not None:
                    handoff_view["verifier_run_id"] = verifier_payload.get(
                        "verifier_run_id"
                    )
                if verifier_payload.get("verifier_report_path") is not None:
                    handoff_view["verifier_report_path"] = verifier_payload.get(
                        "verifier_report_path"
                    )

    duplicate_count = 0
    if store is not None and handoff_view.get("handoff_id"):
        duplicate_count = _count_duplicate_runtime_executions(
            store,
            request.task_key,
            handoff_id=str(handoff_view.get("handoff_id")),
            verifier_report_id=_string_value(handoff_view.get("verifier_report_id")),
            confirmation_id=_string_value(handoff_view.get("confirmation_id")),
            proposal_hash=_string_value(handoff_view.get("proposal_hash")),
            proposal_item_id=_string_value(handoff_view.get("proposal_item_id")),
            item_hash=_string_value(handoff_view.get("item_hash")),
        )
    current_view["duplicate_runtime_execution_count"] = duplicate_count
    if duplicate_count == 0:
        checks["duplicate_runtime_execution_absent"] = True
    else:
        reasons.append(REASON_DUPLICATE_RUNTIME_EXECUTION)

    consumed_events: list[dict[str, Any]] = []
    if store is not None and handoff_view.get("handoff_id"):
        consumed_events = store.list_lineage_consumption_events(
            request.task_key,
            HANDOFF_CONSUMED_EVENT_TYPE,
            consumed_artifact_type=HANDOFF_ARTIFACT_TYPE,
            consumed_artifact_path=_string_value(
                handoff_view.get("handoff_artifact_path")
            ),
            confirmation_id=_string_value(handoff_view.get("confirmation_id")),
            verifier_report_id=_string_value(handoff_view.get("verifier_report_id")),
            handoff_id=_string_value(handoff_view.get("handoff_id")),
            proposal_hash=_string_value(handoff_view.get("proposal_hash")),
            proposal_item_id=_string_value(handoff_view.get("proposal_item_id")),
            item_hash=_string_value(handoff_view.get("item_hash")),
        )
    current_view["intake_runner_handoff_consumed_count"] = len(consumed_events)
    if not consumed_events:
        checks["intake_runner_handoff_not_consumed"] = True
    else:
        reasons.append(REASON_HANDOFF_ALREADY_CONSUMED)

    unique_reasons = list(dict.fromkeys(reasons))
    unique_warnings = list(dict.fromkeys(warnings))
    passed = not unique_reasons and all(checks[name] for name in _REQUIRED_CHECKS)

    return {
        "ok": True,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "mode": "read_only",
        "task_key": request.task_key,
        "preflight_passed": passed,
        "execution_allowed": passed,
        "reasons": unique_reasons,
        "warnings": unique_warnings,
        "handoff": handoff_view,
        "current": current_view,
        "checks": checks,
        "safety": _safety(runtime_started=False, read_only=True),
    }


# --------------------------------------------------------------------------
# Runtime execution
# --------------------------------------------------------------------------


def run_runtime_handoff_execution_from_handoff(
    request: RuntimeHandoffExecutionRequest,
    *,
    approved_task_runner_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run preflight and, when confirmed, call ``approved_task_runner``."""

    if not request.dry_run and not request.confirm_run_approved_task_runner:
        raise RuntimeHandoffExecutionError(
            "Non-dry-run runtime execution requires "
            "confirm_run_approved_task_runner=True"
        )

    preflight = check_runtime_handoff_preflight(request)

    if not preflight.get("preflight_passed"):
        return {
            "ok": False,
            "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
            "source": RUNTIME_EXECUTION_SOURCE,
            "status": "preflight_failed",
            "mode": _mode(request),
            "preflight_passed": False,
            "execution_allowed": False,
            "reasons": list(preflight.get("reasons") or []),
            "preflight": preflight,
            "safety": _safety(runtime_started=False),
        }

    if request.dry_run:
        return {
            "ok": True,
            "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
            "source": RUNTIME_EXECUTION_SOURCE,
            "status": "dry_run",
            "mode": "dry_run",
            "preflight_passed": True,
            "execution_allowed": True,
            "would_call_approved_task_runner": True,
            "preflight": preflight,
            "runtime_execution": None,
            "safety": _safety(runtime_started=False),
        }

    runner_fn = approved_task_runner_fn or _default_approved_task_runner
    runtime_execution_id = _make_runtime_execution_id()
    created_at = utc_now_iso()
    handoff_view = dict(preflight.get("handoff") or {})

    store = TaskMirrorStore(request.db_path)

    preflight_event = _preflight_event_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
        preflight_passed=True,
    )
    store.record_task_event(
        request.task_key,
        RUNTIME_PREFLIGHT_EVENT_TYPE,
        RUNTIME_EXECUTION_SOURCE,
        message=(
            f"Runtime preflight {runtime_execution_id} passed "
            "(runtime audit evidence only)"
        ),
        payload=preflight_event,
    )

    started_event = _started_event_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
    )
    store.record_task_event(
        request.task_key,
        RUNTIME_STARTED_EVENT_TYPE,
        RUNTIME_EXECUTION_SOURCE,
        message=(
            f"Runtime execution {runtime_execution_id} starting "
            "approved_task_runner (runtime audit evidence only)"
        ),
        payload=started_event,
    )

    runner_returned = False
    runner_ok = False
    runner_status: str | None = None
    runner_phase: str | None = None
    runner_error: str | None = None
    runner_summary: dict[str, Any] | None = None
    runner_safety: dict[str, Any] = {}
    try:
        runner_result = runner_fn(
            task_key=request.task_key,
            handoff=handoff_view,
            handoff_id=request.handoff_id,
            runtime_execution_id=runtime_execution_id,
            db_path=request.db_path,
            artifact_root=request.artifact_root,
        )
        runner_returned = True
    except Exception as exc:  # pragma: no cover - defensive runner failure path
        runner_error = f"{exc.__class__.__name__}: {exc}"
        runner_result = None

    if runner_returned:
        runner_view = _coerce_runner_result(runner_result)
        runner_ok = bool(runner_view.get("ok"))
        runner_status = runner_view.get("status")
        runner_phase = runner_view.get("phase")
        if runner_view.get("error") is not None:
            runner_error = str(runner_view.get("error"))
        runner_summary = runner_view.get("summary_payload")
        runner_safety = runner_view.get("safety") or {}

    artifact_payload = _build_runtime_artifact_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
        created_at=created_at,
        preflight=preflight,
        runner_returned=runner_returned,
        runner_ok=runner_ok,
        runner_status=runner_status,
        runner_phase=runner_phase,
        runner_error=runner_error,
        runner_summary=runner_summary,
        runner_safety=runner_safety,
    )
    artifact_path = Path(artifact_payload["artifact_path"])
    atomic_write_json(
        artifact_path,
        artifact_payload,
        sort_keys=True,
        trailing_newline=False,
    )
    store.record_task_artifact(
        request.task_key,
        RUNTIME_EXECUTION_ARTIFACT_TYPE,
        artifact_path,
    )

    finished_event = _finished_event_payload(
        request=request,
        handoff_view=handoff_view,
        runtime_execution_id=runtime_execution_id,
        runner_returned=runner_returned,
        runner_ok=runner_ok,
        runner_status=runner_status,
        runner_phase=runner_phase,
        runner_error=runner_error,
        runtime_execution_artifact_path=artifact_path,
    )
    store.record_task_event(
        request.task_key,
        RUNTIME_FINISHED_EVENT_TYPE,
        RUNTIME_EXECUTION_SOURCE,
        message=(
            f"Runtime execution {runtime_execution_id} finished "
            "(runtime audit evidence only; not approval)"
        ),
        payload=finished_event,
    )
    store.record_lineage_consumed(
        request.task_key,
        HANDOFF_CONSUMED_EVENT_TYPE,
        RUNTIME_EXECUTION_SOURCE,
        consumed_artifact_type=HANDOFF_ARTIFACT_TYPE,
        consumed_artifact_path=str(handoff_view["handoff_artifact_path"]),
        consumer_artifact_type=RUNTIME_EXECUTION_ARTIFACT_TYPE,
        consumer_artifact_path=artifact_path,
        confirmation_id=str(handoff_view["confirmation_id"]),
        verifier_report_id=str(handoff_view["verifier_report_id"]),
        handoff_id=str(handoff_view["handoff_id"]),
        proposal_hash=str(handoff_view["proposal_hash"]),
        proposal_item_id=str(handoff_view["proposal_item_id"]),
        item_hash=str(handoff_view["item_hash"]),
    )

    status = "executed" if runner_returned and runner_ok else "executed_with_failure"
    return {
        "ok": runner_returned and runner_ok,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "source": RUNTIME_EXECUTION_SOURCE,
        "status": status,
        "mode": "confirmed",
        "preflight_passed": True,
        "execution_allowed": True,
        "preflight": preflight,
        "runtime_execution": artifact_payload,
        "safety": _safety(runtime_started=True),
    }


# --------------------------------------------------------------------------
# Default runner
# --------------------------------------------------------------------------


def _default_approved_task_runner(**_kwargs: Any) -> dict[str, Any]:
    """Lazy default runner; raises if no injected fn and no usable task.

    The real ``approved_task_runner`` requires additional inputs (executor,
    repo path, workspace) that the Level 6A contract does not collect.
    Callers are expected to inject ``approved_task_runner_fn`` when they
    need to call into the real runner with the necessary inputs. The
    default returns an error payload so that audit events are still
    recorded but no destructive action is taken.
    """

    return {
        "ok": False,
        "status": "blocked",
        "phase": "no_runner_configured",
        "error": (
            "No approved_task_runner_fn was injected; Level 6A core helper "
            "requires an explicit runner configuration to invoke "
            "approved_task_runner safely."
        ),
        "safety": {
            "executor_started": False,
            "validators_started": False,
            "github_mutated": False,
        },
    }


def _coerce_runner_result(runner_result: Any) -> dict[str, Any]:
    if runner_result is None:
        return {"ok": False, "status": None, "phase": None, "summary_payload": None}
    if hasattr(runner_result, "to_dict"):
        try:
            payload = runner_result.to_dict()  # type: ignore[call-arg]
        except Exception:  # pragma: no cover - defensive
            payload = None
        if isinstance(payload, dict):
            return {
                "ok": bool(payload.get("ok")),
                "status": payload.get("status"),
                "phase": payload.get("phase"),
                "error": payload.get("error"),
                "safety": payload.get("safety") if isinstance(payload.get("safety"), dict) else {},
                "summary_payload": payload.get("summary")
                if isinstance(payload.get("summary"), dict)
                else _summary_from_payload(payload),
            }
    if isinstance(runner_result, dict):
        return {
            "ok": bool(runner_result.get("ok")),
            "status": runner_result.get("status"),
            "phase": runner_result.get("phase"),
            "error": runner_result.get("error"),
            "safety": runner_result.get("safety") if isinstance(runner_result.get("safety"), dict) else {},
            "summary_payload": _summary_from_payload(runner_result),
        }
    return {"ok": False, "status": None, "phase": None, "summary_payload": None}


def _summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keys = ("summary", "status", "phase", "task_key", "executor", "artifacts")
    return {key: payload.get(key) for key in keys if key in payload}


# --------------------------------------------------------------------------
# Helpers: reading handoff evidence
# --------------------------------------------------------------------------


def _read_task_handoff_items(
    store: TaskMirrorStore,
    task_key: str,
) -> list[dict[str, Any]]:
    events = [
        event
        for event in store.list_task_events(task_key)
        if event.event_type == HANDOFF_EVENT_TYPE
    ]
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == HANDOFF_ARTIFACT_TYPE
    ]
    artifacts_by_path = {str(artifact.path): artifact for artifact in artifacts}

    used_artifact_paths: set[str] = set()
    items: list[dict[str, Any]] = []

    for event in events:
        payload, warnings = _parse_event_payload(event.payload_json)
        artifact_path = _string_value(payload.get("artifact_path"))
        artifact = artifacts_by_path.get(artifact_path or "")
        artifact_payload: dict[str, Any] | None = None
        if artifact is not None:
            used_artifact_paths.add(str(artifact.path))
            artifact_payload, artifact_warnings = _read_json_payload(artifact.path)
            warnings.extend(artifact_warnings)
        elif artifact_path:
            artifact_payload, artifact_warnings = _read_json_payload(
                Path(artifact_path)
            )
            warnings.append("artifact_row_missing")
            warnings.extend(artifact_warnings)
        else:
            warnings.append("artifact_payload_missing")

        items.append(
            _normalize_handoff_item(
                event_payload=payload,
                artifact_payload=artifact_payload,
                artifact_path=artifact_path,
                warnings=warnings,
            )
        )

    for artifact in artifacts:
        artifact_path = str(artifact.path)
        if artifact_path in used_artifact_paths:
            continue
        artifact_payload, warnings = _read_json_payload(artifact.path)
        items.append(
            _normalize_handoff_item(
                event_payload={},
                artifact_payload=artifact_payload,
                artifact_path=artifact_path,
                warnings=warnings,
            )
        )
    return items


def _normalize_handoff_item(
    *,
    event_payload: dict[str, Any],
    artifact_payload: dict[str, Any] | None,
    artifact_path: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    payload = artifact_payload or {}
    resolved_artifact_path = _first_string(
        artifact_path,
        payload.get("artifact_path"),
        event_payload.get("artifact_path"),
    )
    return {
        "handoff_id": _first_string(
            event_payload.get("handoff_id"),
            payload.get("handoff_id"),
        ),
        "verifier_report_id": _first_string(
            event_payload.get("verifier_report_id"),
            payload.get("verifier_report_id"),
        ),
        "confirmation_id": _first_string(
            event_payload.get("confirmation_id"),
            payload.get("confirmation_id"),
        ),
        "proposal_hash": _first_string(
            event_payload.get("proposal_hash"),
            payload.get("proposal_hash"),
        ),
        "proposal_item_id": _first_string(
            event_payload.get("proposal_item_id"),
            payload.get("proposal_item_id"),
        ),
        "item_hash": _first_string(
            event_payload.get("item_hash"),
            payload.get("item_hash"),
        ),
        "recommended_command_kind": _first_string(
            event_payload.get("recommended_command_kind"),
            payload.get("recommended_command_kind"),
        ),
        "verifier_report_artifact_path": _first_string(
            event_payload.get("verifier_report_artifact_path"),
            payload.get("verifier_report_artifact_path"),
        ),
        "confirmation_artifact_path": _first_string(
            event_payload.get("confirmation_artifact_path"),
            payload.get("confirmation_artifact_path"),
        ),
        "proposal_artifact_path": _first_string(
            event_payload.get("proposal_artifact_path"),
            payload.get("proposal_artifact_path"),
        ),
        "artifact_path": resolved_artifact_path,
        "readback_warnings": list(dict.fromkeys(warnings)),
    }


def _filter_handoff_items(
    items: list[dict[str, Any]],
    request: RuntimeHandoffExecutionRequest,
) -> list[dict[str, Any]]:
    expected_artifact_path = (
        str(request.handoff_artifact_path)
        if request.handoff_artifact_path is not None
        else None
    )
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("handoff_id") != request.handoff_id:
            continue
        if (
            request.verifier_report_id is not None
            and item.get("verifier_report_id") != request.verifier_report_id
        ):
            continue
        if (
            request.confirmation_id is not None
            and item.get("confirmation_id") != request.confirmation_id
        ):
            continue
        if (
            request.proposal_hash is not None
            and item.get("proposal_hash") != request.proposal_hash
        ):
            continue
        if (
            request.proposal_item_id is not None
            and item.get("proposal_item_id") != request.proposal_item_id
        ):
            continue
        if (
            request.item_hash is not None
            and item.get("item_hash") != request.item_hash
        ):
            continue
        if (
            request.recommended_command_kind is not None
            and item.get("recommended_command_kind")
            != request.recommended_command_kind
        ):
            continue
        if (
            expected_artifact_path is not None
            and item.get("artifact_path") != expected_artifact_path
        ):
            continue
        matches.append(item)
    return matches


def _copy_selected_handoff(
    selected: dict[str, Any], handoff_view: dict[str, Any]
) -> None:
    for key in (
        "handoff_id",
        "verifier_report_id",
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
        "verifier_report_artifact_path",
        "confirmation_artifact_path",
        "proposal_artifact_path",
    ):
        if selected.get(key) is not None:
            handoff_view[key] = selected.get(key)
    if selected.get("artifact_path") is not None:
        handoff_view["handoff_artifact_path"] = selected.get("artifact_path")


def _populate_handoff_view_from_payload(
    payload: dict[str, Any], handoff_view: dict[str, Any]
) -> None:
    for key in (
        "handoff_id",
        "verifier_report_id",
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
        "verifier_report_artifact_path",
        "confirmation_artifact_path",
        "proposal_artifact_path",
    ):
        value = _string_value(payload.get(key))
        if value is not None:
            handoff_view[key] = value
    artifact_path = _string_value(payload.get("artifact_path"))
    if artifact_path is not None:
        handoff_view["handoff_artifact_path"] = artifact_path


def _verify_handoff_safety_flags(
    *,
    payload: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    if (
        payload.get("not_execution_permission") is True
        and payload.get("not_runtime") is True
        and payload.get("approved_task_runner_called") is False
        and payload.get("requires_runtime_preflight") is True
        and payload.get("requires_next_gate") is True
    ):
        checks["handoff_safety_flags_valid"] = True
    else:
        reasons.append(REASON_HANDOFF_SAFETY_FLAGS_INVALID)


def _verify_handoff_binding(
    *,
    payload: dict[str, Any],
    selected: dict[str, Any],
    request: RuntimeHandoffExecutionRequest,
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    selected_matches_payload = all(
        _string_value(selected.get(key)) == _string_value(payload.get(key))
        for key in (
            "verifier_report_id",
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "verifier_report_artifact_path",
            "confirmation_artifact_path",
            "proposal_artifact_path",
        )
    )
    request_matches_payload = all(
        expected is None or _string_value(payload.get(key)) == expected
        for key, expected in (
            ("verifier_report_id", request.verifier_report_id),
            ("confirmation_id", request.confirmation_id),
            ("proposal_hash", request.proposal_hash),
            ("proposal_item_id", request.proposal_item_id),
            ("item_hash", request.item_hash),
            ("recommended_command_kind", request.recommended_command_kind),
        )
    )
    required_bindings_present = all(
        _string_value(payload.get(key)) is not None
        for key in _HANDOFF_REQUIRED_BINDING_KEYS
    )
    artifact_path_matches = (
        request.handoff_artifact_path is None
        or _string_value(payload.get("artifact_path"))
        == str(request.handoff_artifact_path)
    )
    handoff_id_matches = _string_value(payload.get("handoff_id")) == request.handoff_id
    if (
        selected_matches_payload
        and request_matches_payload
        and required_bindings_present
        and artifact_path_matches
        and handoff_id_matches
    ):
        checks["handoff_binding_matches"] = True
    else:
        reasons.append(REASON_HANDOFF_BINDING_MISMATCH)


def _verify_confirmation_payload(
    *,
    payload: dict[str, Any],
    verifier: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    bindings_match = all(
        _string_value(payload.get(key)) == _string_value(verifier.get(key))
        for key in (
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "proposal_artifact_path",
        )
    )
    expected_artifact_path = _string_value(verifier.get("confirmation_artifact_path"))
    artifact_path_matches = (
        expected_artifact_path is not None
        and _string_value(payload.get("artifact_path")) == expected_artifact_path
    )
    if bindings_match and artifact_path_matches:
        pass
    else:
        reasons.append(REASON_CONFIRMATION_BINDING_MISMATCH)
        return


def _verify_proposal_payload(
    *,
    verifier: dict[str, Any],
    proposal_item: dict[str, Any] | None,
    hash_report: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    verifier_proposal_hash = _string_value(verifier.get("proposal_hash"))
    if (
        verifier_proposal_hash
        and hash_report.get("proposal_hash_valid") is True
        and hash_report.get("actual_proposal_hash") == verifier_proposal_hash
    ):
        checks["proposal_hash_matches_artifact"] = True
    else:
        reasons.append(REASON_PROPOSAL_HASH_MISMATCH)

    if proposal_item is None:
        reasons.append(REASON_PROPOSAL_ITEM_ID_MISSING_FROM_ARTIFACT)
        return

    checks["proposal_item_id_exists"] = True
    item_report = _item_hash_report(
        hash_report,
        _string_value(verifier.get("proposal_item_id")),
    )
    verifier_item_hash = _string_value(verifier.get("item_hash"))
    if (
        verifier_item_hash
        and item_report is not None
        and item_report.get("item_hash_valid") is True
        and item_report.get("actual_item_hash") == verifier_item_hash
        and _string_value(proposal_item.get("item_hash")) == verifier_item_hash
    ):
        checks["item_hash_matches_selected_item"] = True
    else:
        reasons.append(REASON_ITEM_HASH_MISMATCH)


def _verify_current_task_and_kind(
    *,
    verifier: dict[str, Any],
    confirmation_payload: dict[str, Any] | None,
    proposal_item: dict[str, Any] | None,
    task_record: Any,
    current_view: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    expected_status = _resolve_expected_status(proposal_item)
    current_view["expected_status"] = expected_status
    if task_record is not None:
        if expected_status is None or task_record.status == expected_status:
            checks["task_status_matches_expected"] = True
        else:
            reasons.append(REASON_TASK_STATUS_MISMATCH)

    verifier_kind = _string_value(verifier.get("recommended_command_kind"))
    confirmation_kind = (
        _string_value(confirmation_payload.get("recommended_command_kind"))
        if confirmation_payload is not None
        else None
    )
    proposal_kind = (
        _string_value(proposal_item.get("recommended_command_kind"))
        if proposal_item is not None
        else None
    )
    if (
        verifier_kind
        and confirmation_kind == verifier_kind
        and proposal_kind == verifier_kind
    ):
        checks["recommended_command_kind_matches"] = True
    else:
        reasons.append(REASON_RECOMMENDED_COMMAND_KIND_MISMATCH)


def _find_proposal_item(
    payload: dict[str, Any], proposal_item_id: str | None
) -> dict[str, Any] | None:
    if not proposal_item_id:
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None
    for item in raw_items:
        if isinstance(item, dict) and item.get("proposal_item_id") == proposal_item_id:
            return item
    return None


def _item_hash_report(
    hash_report: dict[str, Any], proposal_item_id: str | None
) -> dict[str, Any] | None:
    if not proposal_item_id:
        return None
    for entry in hash_report.get("items") or []:
        if (
            isinstance(entry, dict)
            and entry.get("proposal_item_id") == proposal_item_id
        ):
            return entry
    return None


def _resolve_expected_status(proposal_item: dict[str, Any] | None) -> str | None:
    if proposal_item is None:
        return None
    expected = _string_value(proposal_item.get("expected_status"))
    if expected:
        return expected
    return _string_value(proposal_item.get("status"))


def _count_duplicate_runtime_executions(
    store: TaskMirrorStore,
    task_key: str,
    *,
    handoff_id: str,
    verifier_report_id: str | None,
    confirmation_id: str | None,
    proposal_hash: str | None,
    proposal_item_id: str | None,
    item_hash: str | None,
) -> int:
    count = 0
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type != RUNTIME_EXECUTION_ARTIFACT_TYPE:
            continue
        payload, _ = _read_json_payload(artifact.path)
        if _matches_runtime_binding(
            payload,
            handoff_id=handoff_id,
            verifier_report_id=verifier_report_id,
            confirmation_id=confirmation_id,
            proposal_hash=proposal_hash,
            proposal_item_id=proposal_item_id,
            item_hash=item_hash,
        ):
            count += 1
    for event in store.list_task_events(task_key):
        if event.event_type not in (
            RUNTIME_STARTED_EVENT_TYPE,
            RUNTIME_FINISHED_EVENT_TYPE,
        ):
            continue
        payload, _ = _parse_event_payload(event.payload_json)
        if _matches_runtime_binding(
            payload,
            handoff_id=handoff_id,
            verifier_report_id=verifier_report_id,
            confirmation_id=confirmation_id,
            proposal_hash=proposal_hash,
            proposal_item_id=proposal_item_id,
            item_hash=item_hash,
        ):
            count += 1
    return count


def _matches_runtime_binding(
    payload: dict[str, Any] | None,
    *,
    handoff_id: str,
    verifier_report_id: str | None,
    confirmation_id: str | None,
    proposal_hash: str | None,
    proposal_item_id: str | None,
    item_hash: str | None,
) -> bool:
    if not payload:
        return False
    if payload.get("handoff_id") != handoff_id:
        return False
    for key, expected in (
        ("verifier_report_id", verifier_report_id),
        ("confirmation_id", confirmation_id),
        ("proposal_hash", proposal_hash),
        ("proposal_item_id", proposal_item_id),
        ("item_hash", item_hash),
    ):
        if expected is not None and payload.get(key) != expected:
            return False
    return True


# --------------------------------------------------------------------------
# Helpers: artifact / event payloads
# --------------------------------------------------------------------------


def _build_runtime_artifact_payload(
    *,
    request: RuntimeHandoffExecutionRequest,
    handoff_view: dict[str, Any],
    runtime_execution_id: str,
    created_at: str,
    preflight: dict[str, Any],
    runner_returned: bool,
    runner_ok: bool,
    runner_status: str | None,
    runner_phase: str | None,
    runner_error: str | None,
    runner_summary: dict[str, Any] | None,
    runner_safety: dict[str, Any],
) -> dict[str, Any]:
    artifact_path = (
        request.artifact_root
        / "runtime_handoff_executions"
        / runtime_execution_id
        / "runtime_handoff_execution.json"
    )
    safety = _safety(runtime_started=True)
    if runner_safety.get("executor_started") is True:
        safety["executor_started"] = True
    if runner_safety.get("validators_started") is True:
        safety["validators_started"] = True
    if runner_safety.get("github_mutated") is True or (
        runner_safety.get("branch_pushed") is True
        or runner_safety.get("pr_created") is True
    ):
        safety["github_mutated"] = True

    return {
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "created_at": created_at,
        "source": RUNTIME_EXECUTION_SOURCE,
        "mode": "confirmed",
        "task_key": request.task_key,
        "handoff_id": handoff_view.get("handoff_id"),
        "verifier_report_id": handoff_view.get("verifier_report_id"),
        "confirmation_id": handoff_view.get("confirmation_id"),
        "proposal_hash": handoff_view.get("proposal_hash"),
        "proposal_item_id": handoff_view.get("proposal_item_id"),
        "item_hash": handoff_view.get("item_hash"),
        "recommended_command_kind": handoff_view.get("recommended_command_kind"),
        "handoff_artifact_path": handoff_view.get("handoff_artifact_path"),
        "verifier_report_artifact_path": handoff_view.get(
            "verifier_report_artifact_path"
        ),
        "confirmation_artifact_path": handoff_view.get("confirmation_artifact_path"),
        "proposal_artifact_path": handoff_view.get("proposal_artifact_path"),
        "verifier_run_id": handoff_view.get("verifier_run_id"),
        "verifier_report_path": handoff_view.get("verifier_report_path"),
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path),
        "operator": request.operator,
        "operator_note": request.operator_note,
        "preflight_passed": True,
        "approved_task_runner_called": True,
        "runner_returned": runner_returned,
        "runner_ok": runner_ok,
        "runner_status": runner_status,
        "runner_phase": runner_phase,
        "runner_error": runner_error,
        "runner_result_summary": runner_summary,
        "checks": dict(preflight.get("checks") or {}),
        "reasons": list(preflight.get("reasons") or []),
        "warnings": list(preflight.get("warnings") or []),
        "safety": safety,
        "not_approval": True,
        "not_merge": True,
        "not_cleanup": True,
    }


def _preflight_event_payload(
    *,
    request: RuntimeHandoffExecutionRequest,
    handoff_view: dict[str, Any],
    runtime_execution_id: str,
    preflight_passed: bool,
) -> dict[str, Any]:
    return {
        "kind": RUNTIME_PREFLIGHT_EVENT_TYPE,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "task_key": request.task_key,
        "handoff_id": handoff_view.get("handoff_id"),
        "verifier_report_id": handoff_view.get("verifier_report_id"),
        "confirmation_id": handoff_view.get("confirmation_id"),
        "proposal_hash": handoff_view.get("proposal_hash"),
        "proposal_item_id": handoff_view.get("proposal_item_id"),
        "item_hash": handoff_view.get("item_hash"),
        "recommended_command_kind": handoff_view.get("recommended_command_kind"),
        "verifier_run_id": handoff_view.get("verifier_run_id"),
        "verifier_report_path": handoff_view.get("verifier_report_path"),
        "intake_runner_handoff_artifact_path": handoff_view.get(
            "handoff_artifact_path"
        ),
        "preflight_passed": preflight_passed,
        "approved_task_runner_invoked": False,
        "not_action_evidence": True,
    }


def _started_event_payload(
    *,
    request: RuntimeHandoffExecutionRequest,
    handoff_view: dict[str, Any],
    runtime_execution_id: str,
) -> dict[str, Any]:
    return {
        "kind": RUNTIME_STARTED_EVENT_TYPE,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "task_key": request.task_key,
        "handoff_id": handoff_view.get("handoff_id"),
        "verifier_report_id": handoff_view.get("verifier_report_id"),
        "confirmation_id": handoff_view.get("confirmation_id"),
        "proposal_hash": handoff_view.get("proposal_hash"),
        "proposal_item_id": handoff_view.get("proposal_item_id"),
        "item_hash": handoff_view.get("item_hash"),
        "recommended_command_kind": handoff_view.get("recommended_command_kind"),
        "verifier_run_id": handoff_view.get("verifier_run_id"),
        "verifier_report_path": handoff_view.get("verifier_report_path"),
        "intake_runner_handoff_artifact_path": handoff_view.get(
            "handoff_artifact_path"
        ),
        "approved_task_runner_invoked": True,
        "not_action_evidence": True,
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _finished_event_payload(
    *,
    request: RuntimeHandoffExecutionRequest,
    handoff_view: dict[str, Any],
    runtime_execution_id: str,
    runner_returned: bool,
    runner_ok: bool,
    runner_status: str | None,
    runner_phase: str | None,
    runner_error: str | None,
    runtime_execution_artifact_path: Path,
) -> dict[str, Any]:
    return {
        "kind": RUNTIME_FINISHED_EVENT_TYPE,
        "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
        "runtime_execution_id": runtime_execution_id,
        "task_key": request.task_key,
        "handoff_id": handoff_view.get("handoff_id"),
        "verifier_report_id": handoff_view.get("verifier_report_id"),
        "confirmation_id": handoff_view.get("confirmation_id"),
        "proposal_hash": handoff_view.get("proposal_hash"),
        "proposal_item_id": handoff_view.get("proposal_item_id"),
        "item_hash": handoff_view.get("item_hash"),
        "recommended_command_kind": handoff_view.get("recommended_command_kind"),
        "verifier_run_id": handoff_view.get("verifier_run_id"),
        "verifier_report_path": handoff_view.get("verifier_report_path"),
        "intake_runner_handoff_artifact_path": handoff_view.get(
            "handoff_artifact_path"
        ),
        "approved_task_runner_invoked": True,
        "runner_returned": runner_returned,
        "runner_ok": runner_ok,
        "runner_status": runner_status,
        "runner_phase": runner_phase,
        "runner_error": runner_error,
        "final_status": runner_status,
        "runtime_execution_artifact_path": str(runtime_execution_artifact_path),
        "not_action_evidence": True,
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _safety(
    *,
    runtime_started: bool,
    read_only: bool | None = None,
) -> dict[str, bool]:
    safety = dict(RUNTIME_EXECUTION_SAFETY_FLAGS)
    safety["runtime_started"] = runtime_started
    safety["approved_task_runner_called"] = runtime_started
    if read_only is not None:
        safety["read_only"] = read_only
    return safety


def _mode(request: RuntimeHandoffExecutionRequest) -> str:
    return "dry_run" if request.dry_run else "confirmed"


def _make_runtime_execution_id() -> str:
    timestamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "")
    digest = hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest()[:12]
    return f"runtime-execution-{timestamp}-{digest}"


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------


def _load_json_object(
    artifact_path: Any,
    *,
    path_missing_reason: str,
    file_missing_reason: str,
    json_malformed_reason: str,
    json_not_object_reason: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    path_text = _string_value(artifact_path)
    if not path_text:
        return None, [path_missing_reason]
    path = Path(path_text)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None, [file_missing_reason]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, [json_malformed_reason]
    if not isinstance(payload, dict):
        return None, [json_not_object_reason]
    return payload, []


def _read_json_payload(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None, ["artifact_file_missing"]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, ["artifact_json_malformed"]
    if not isinstance(payload, dict):
        return None, ["artifact_json_not_object"]
    return payload, []


def _parse_event_payload(payload_json: str | None) -> tuple[dict[str, Any], list[str]]:
    if not payload_json:
        return {}, ["event_payload_missing"]
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}, ["event_payload_malformed"]
    if not isinstance(payload, dict):
        return {}, ["event_payload_not_object"]
    return payload, []


def _first_string(*values: Any) -> str | None:
    for value in values:
        string = _string_value(value)
        if string is not None:
            return string
    return None


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


__all__ = [
    "RUNTIME_EXECUTION_ARTIFACT_TYPE",
    "RUNTIME_EXECUTION_SAFETY_FLAGS",
    "RUNTIME_EXECUTION_SCHEMA_VERSION",
    "RUNTIME_EXECUTION_SOURCE",
    "HANDOFF_CONSUMED_EVENT_TYPE",
    "RUNTIME_FINISHED_EVENT_TYPE",
    "RUNTIME_PREFLIGHT_EVENT_TYPE",
    "RUNTIME_STARTED_EVENT_TYPE",
    "RuntimeHandoffExecutionError",
    "RuntimeHandoffExecutionRequest",
    "check_runtime_handoff_preflight",
    "run_runtime_handoff_execution_from_handoff",
]
