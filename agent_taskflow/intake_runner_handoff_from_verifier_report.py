"""Level 5A minimal intake runner handoff from verifier report.

This module converts an already recorded
``scheduler_confirmation_verifier_report`` into local handoff evidence
after an explicit operator command. The binding helper is read-only and
the creation helper is dry-run by default.

An ``intake_runner_handoff`` created here is prepared evidence for the
next runtime preflight gate only. It is not runtime execution, not
execution permission, not proof that the approved task runner was called,
not executor or validator evidence, and not approval, merge, or cleanup.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import utc_now_iso
from agent_taskflow.scheduler_confirmation_verifier_report import (
    VERIFIER_REPORT_ARTIFACT_TYPE,
    VERIFIER_REPORT_EVENT_TYPE,
)
from agent_taskflow.scheduler_proposals import verify_proposal_hashes
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


HANDOFF_SCHEMA_VERSION = "intake_runner_handoff_from_verifier_report.v1"
HANDOFF_SOURCE = "intake_runner_handoff_from_verifier_report"
HANDOFF_ARTIFACT_TYPE = "intake_runner_handoff"
HANDOFF_EVENT_TYPE = "intake_runner_handoff_created"
VERIFIER_REPORT_CONSUMED_EVENT_TYPE = (
    "scheduler_confirmation_verifier_report_consumed"
)

HANDOFF_SAFETY_FLAGS: dict[str, bool] = {
    "handoff_created": False,
    "runtime_started": False,
    "approved_task_runner_called": False,
    "executor_started": False,
    "validators_started": False,
    "github_mutated": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "background_worker_started": False,
    "not_execution_permission": True,
    "not_runtime": True,
    "requires_runtime_preflight": True,
    "requires_next_gate": True,
}

REASON_TASK_MISSING = "task_missing"
REASON_VERIFIER_REPORT_NOT_FOUND = "verifier_report_not_found"
REASON_VERIFIER_REPORT_AMBIGUOUS = "verifier_report_ambiguous"
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
REASON_VERIFIER_REPORT_BINDING_MISMATCH = "verifier_report_binding_mismatch"
REASON_CONFIRMATION_ARTIFACT_PATH_MISSING = "confirmation_artifact_path_missing"
REASON_CONFIRMATION_ARTIFACT_FILE_MISSING = "confirmation_artifact_file_missing"
REASON_CONFIRMATION_ARTIFACT_JSON_MALFORMED = (
    "confirmation_artifact_json_malformed"
)
REASON_CONFIRMATION_ARTIFACT_JSON_NOT_OBJECT = (
    "confirmation_artifact_json_not_object"
)
REASON_CONFIRMATION_BINDING_MISMATCH = "confirmation_binding_mismatch"
REASON_PROPOSAL_ARTIFACT_PATH_MISSING = "proposal_artifact_path_missing"
REASON_PROPOSAL_ARTIFACT_FILE_MISSING = "proposal_artifact_file_missing"
REASON_PROPOSAL_ARTIFACT_JSON_MALFORMED = "proposal_artifact_json_malformed"
REASON_PROPOSAL_ARTIFACT_JSON_NOT_OBJECT = "proposal_artifact_json_not_object"
REASON_PROPOSAL_HASH_MISMATCH = "proposal_hash_mismatch"
REASON_PROPOSAL_ITEM_ID_MISSING_FROM_ARTIFACT = (
    "proposal_item_id_missing_from_artifact"
)
REASON_ITEM_HASH_MISMATCH = "item_hash_mismatch"
REASON_TASK_STATUS_MISMATCH = "task_status_mismatch"
REASON_RECOMMENDED_COMMAND_KIND_MISMATCH = "recommended_command_kind_mismatch"
REASON_DUPLICATE_ACTIVE_HANDOFF = "duplicate_active_handoff"
REASON_VERIFIER_REPORT_ALREADY_CONSUMED = (
    "scheduler_confirmation_verifier_report_already_consumed"
)

_REQUIRED_CHECKS: tuple[str, ...] = (
    "verifier_report_exists",
    "verifier_report_artifact_exists",
    "verifier_report_passed",
    "verifier_report_binding_matches",
    "confirmation_artifact_exists",
    "confirmation_binding_matches",
    "proposal_artifact_exists",
    "proposal_hash_matches_artifact",
    "proposal_item_id_exists",
    "item_hash_matches_selected_item",
    "task_still_exists",
    "task_status_matches_expected",
    "recommended_command_kind_matches",
    "duplicate_handoff_absent",
    "scheduler_confirmation_verifier_report_not_consumed",
)

_VERIFIER_REQUIRED_BINDING_KEYS: tuple[str, ...] = (
    "verifier_report_id",
    "confirmation_id",
    "proposal_hash",
    "proposal_item_id",
    "item_hash",
    "recommended_command_kind",
    "confirmation_artifact_path",
    "proposal_artifact_path",
)


class IntakeRunnerHandoffFromVerifierReportError(RuntimeError):
    """Raised when Level 5A handoff creation cannot proceed safely."""


@dataclass(frozen=True)
class IntakeRunnerHandoffFromVerifierReportRequest:
    """Inputs to the Level 5A read-only binding and handoff creator."""

    db_path: Path
    artifact_root: Path
    task_key: str
    verifier_report_id: str
    confirmation_id: str | None = None
    proposal_hash: str | None = None
    proposal_item_id: str | None = None
    item_hash: str | None = None
    recommended_command_kind: str | None = None
    verifier_report_artifact_path: Path | None = None
    dry_run: bool = True
    confirm_create_handoff: bool = False
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

        verifier_report_id = (self.verifier_report_id or "").strip()
        if not verifier_report_id:
            raise ValueError("verifier_report_id must not be empty")
        object.__setattr__(self, "verifier_report_id", verifier_report_id)

        for field_name in (
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

        if self.verifier_report_artifact_path is not None:
            object.__setattr__(
                self,
                "verifier_report_artifact_path",
                Path(self.verifier_report_artifact_path).expanduser(),
            )


def check_intake_runner_handoff_binding(
    request: IntakeRunnerHandoffFromVerifierReportRequest,
) -> dict[str, Any]:
    """Return a read-only Level 5A handoff binding report."""

    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {name: False for name in _REQUIRED_CHECKS}

    verifier_view: dict[str, Any] = {
        "verifier_report_id": request.verifier_report_id,
        "confirmation_id": None,
        "proposal_hash": None,
        "proposal_item_id": None,
        "item_hash": None,
        "recommended_command_kind": None,
        "verifier_report_artifact_path": None,
        "confirmation_artifact_path": None,
        "proposal_artifact_path": None,
    }
    current_view: dict[str, Any] = {
        "task_exists": False,
        "task_status": None,
        "expected_status": None,
        "duplicate_handoff_count": 0,
        "scheduler_confirmation_verifier_report_consumed_count": 0,
    }

    task_record = None
    items: list[dict[str, Any]] = []
    store: TaskMirrorStore | None = None
    if request.db_path.exists():
        store = TaskMirrorStore(request.db_path)
        task_record = store.get_task(request.task_key)
        if task_record is not None:
            checks["task_still_exists"] = True
            current_view["task_exists"] = True
            current_view["task_status"] = task_record.status
            items = _read_task_verifier_report_items(store, request.task_key)
        else:
            reasons.append(REASON_TASK_MISSING)
    else:
        reasons.append(REASON_TASK_MISSING)

    matches = _filter_verifier_report_items(items, request)

    verifier_payload: dict[str, Any] | None = None
    confirmation_payload: dict[str, Any] | None = None
    proposal_payload: dict[str, Any] | None = None
    proposal_item: dict[str, Any] | None = None

    if not matches:
        reasons.append(REASON_VERIFIER_REPORT_NOT_FOUND)
    elif len(matches) > 1:
        reasons.append(REASON_VERIFIER_REPORT_AMBIGUOUS)
    else:
        selected = matches[0]
        checks["verifier_report_exists"] = True
        warnings.extend(selected.get("readback_warnings") or [])
        _copy_selected_verifier_report(selected, verifier_view)

        verifier_payload, verifier_reasons = _load_verifier_report_artifact(
            selected.get("artifact_path")
        )
        reasons.extend(verifier_reasons)
        if verifier_payload is not None:
            checks["verifier_report_artifact_exists"] = True
            _verify_verifier_report_payload(
                payload=verifier_payload,
                selected=selected,
                request=request,
                verifier_view=verifier_view,
                checks=checks,
                reasons=reasons,
            )

            confirmation_payload, confirmation_reasons = (
                _load_confirmation_artifact(
                    verifier_payload.get("confirmation_artifact_path")
                )
            )
            reasons.extend(confirmation_reasons)
            if confirmation_payload is not None:
                checks["confirmation_artifact_exists"] = True
                _verify_confirmation_payload(
                    payload=confirmation_payload,
                    verifier=verifier_view,
                    checks=checks,
                    reasons=reasons,
                )

            proposal_payload, proposal_reasons = _load_proposal_artifact(
                verifier_payload.get("proposal_artifact_path")
            )
            reasons.extend(proposal_reasons)
            if proposal_payload is not None:
                checks["proposal_artifact_exists"] = True
                hash_report = verify_proposal_hashes(proposal_payload)
                proposal_item = _find_proposal_item(
                    proposal_payload,
                    _string_value(verifier_view.get("proposal_item_id")),
                )
                _verify_proposal_payload(
                    payload=proposal_payload,
                    verifier=verifier_view,
                    proposal_item=proposal_item,
                    hash_report=hash_report,
                    checks=checks,
                    reasons=reasons,
                )
                _verify_current_task_and_kind(
                    verifier=verifier_view,
                    confirmation_payload=confirmation_payload,
                    proposal_item=proposal_item,
                    task_record=task_record,
                    current_view=current_view,
                    checks=checks,
                    reasons=reasons,
                )

    duplicate_count = 0
    if store is not None:
        duplicate_count = _count_duplicate_handoffs(
            store,
            request.task_key,
            verifier_report_id=request.verifier_report_id,
            confirmation_id=_string_value(verifier_view.get("confirmation_id"))
            or request.confirmation_id,
            proposal_hash=_string_value(verifier_view.get("proposal_hash"))
            or request.proposal_hash,
            proposal_item_id=_string_value(verifier_view.get("proposal_item_id"))
            or request.proposal_item_id,
            item_hash=_string_value(verifier_view.get("item_hash"))
            or request.item_hash,
        )
    current_view["duplicate_handoff_count"] = duplicate_count
    if duplicate_count == 0:
        checks["duplicate_handoff_absent"] = True
    else:
        reasons.append(REASON_DUPLICATE_ACTIVE_HANDOFF)

    consumed_events: list[dict[str, Any]] = []
    if store is not None:
        consumed_events = store.list_lineage_consumption_events(
            request.task_key,
            VERIFIER_REPORT_CONSUMED_EVENT_TYPE,
            consumed_artifact_type=VERIFIER_REPORT_ARTIFACT_TYPE,
            consumed_artifact_path=_string_value(
                verifier_view.get("verifier_report_artifact_path")
            ),
            confirmation_id=_string_value(verifier_view.get("confirmation_id"))
            or request.confirmation_id,
            verifier_report_id=request.verifier_report_id,
            proposal_hash=_string_value(verifier_view.get("proposal_hash"))
            or request.proposal_hash,
            proposal_item_id=_string_value(verifier_view.get("proposal_item_id"))
            or request.proposal_item_id,
            item_hash=_string_value(verifier_view.get("item_hash")) or request.item_hash,
        )
    current_view["scheduler_confirmation_verifier_report_consumed_count"] = len(
        consumed_events
    )
    if not consumed_events:
        checks["scheduler_confirmation_verifier_report_not_consumed"] = True
    else:
        reasons.append(REASON_VERIFIER_REPORT_ALREADY_CONSUMED)

    unique_reasons = list(dict.fromkeys(reasons))
    unique_warnings = list(dict.fromkeys(warnings))
    allowed = not unique_reasons and all(checks[name] for name in _REQUIRED_CHECKS)

    return {
        "ok": True,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "mode": "read_only",
        "task_key": request.task_key,
        "handoff_allowed": allowed,
        "eligible_for_handoff": allowed,
        "reasons": unique_reasons,
        "warnings": unique_warnings,
        "verifier_report": verifier_view,
        "current": current_view,
        "checks": checks,
        "safety": _safety(handoff_created=False, read_only=True),
    }


def create_intake_runner_handoff_from_verifier_report(
    request: IntakeRunnerHandoffFromVerifierReportRequest,
) -> dict[str, Any]:
    """Create Level 5A handoff evidence after explicit confirmation."""

    if not request.dry_run and not request.confirm_create_handoff:
        raise IntakeRunnerHandoffFromVerifierReportError(
            "Non-dry-run intake runner handoff creation requires "
            "confirm_create_handoff=True"
        )

    binding = check_intake_runner_handoff_binding(request)
    if not binding.get("handoff_allowed"):
        return {
            "ok": False,
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "source": HANDOFF_SOURCE,
            "status": "not_allowed",
            "mode": _mode(request),
            "handoff_allowed": False,
            "reasons": list(binding.get("reasons") or []),
            "binding": binding,
            "safety": _safety(handoff_created=False),
        }

    handoff = _build_handoff_payload(
        request,
        binding=binding,
        mode=_mode(request),
        handoff_created=not request.dry_run,
    )

    if request.dry_run:
        return {
            "ok": True,
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "source": HANDOFF_SOURCE,
            "status": "dry_run",
            "mode": "dry_run",
            "would_create_handoff": True,
            "handoff": handoff,
            "binding": binding,
            "safety": _safety(handoff_created=False),
        }

    artifact_path = Path(handoff["artifact_path"])
    atomic_write_json(artifact_path, handoff, sort_keys=True, trailing_newline=False)

    store = TaskMirrorStore(request.db_path)
    store.record_task_artifact(
        request.task_key,
        HANDOFF_ARTIFACT_TYPE,
        artifact_path,
    )
    event_payload = _event_payload(handoff)
    store.record_task_event(
        request.task_key,
        HANDOFF_EVENT_TYPE,
        HANDOFF_SOURCE,
        message=(
            f"Intake runner handoff {handoff['handoff_id']} recorded "
            "(prepared evidence only; not runtime execution)"
        ),
        payload=event_payload,
    )
    store.record_lineage_consumed(
        request.task_key,
        VERIFIER_REPORT_CONSUMED_EVENT_TYPE,
        HANDOFF_SOURCE,
        consumed_artifact_type=VERIFIER_REPORT_ARTIFACT_TYPE,
        consumed_artifact_path=str(handoff["verifier_report_artifact_path"]),
        consumer_artifact_type=HANDOFF_ARTIFACT_TYPE,
        consumer_artifact_path=artifact_path,
        confirmation_id=str(handoff["confirmation_id"]),
        verifier_report_id=str(handoff["verifier_report_id"]),
        handoff_id=str(handoff["handoff_id"]),
        proposal_hash=str(handoff["proposal_hash"]),
        proposal_item_id=str(handoff["proposal_item_id"]),
        item_hash=str(handoff["item_hash"]),
    )

    return {
        "ok": True,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "source": HANDOFF_SOURCE,
        "status": "created",
        "mode": "confirmed",
        "handoff_allowed": True,
        "handoff": handoff,
        "binding": binding,
        "safety": _safety(handoff_created=True),
    }


def _read_task_verifier_report_items(
    store: TaskMirrorStore,
    task_key: str,
) -> list[dict[str, Any]]:
    events = [
        event
        for event in store.list_task_events(task_key)
        if event.event_type == VERIFIER_REPORT_EVENT_TYPE
    ]
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == VERIFIER_REPORT_ARTIFACT_TYPE
    ]
    artifacts_by_path = {str(artifact.path): artifact for artifact in artifacts}

    used_artifact_paths: set[str] = set()
    items: list[dict[str, Any]] = []

    for event in events:
        payload, warnings = _parse_event_payload(event.payload_json)
        artifact_path = _string_value(payload.get("artifact_path"))
        artifact = artifacts_by_path.get(artifact_path or "")
        artifact_created_at = artifact.created_at if artifact is not None else None

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
            _normalize_verifier_report_item(
                task_key=task_key,
                event_payload=payload,
                artifact_payload=artifact_payload,
                artifact_path=artifact_path,
                artifact_created_at=artifact_created_at,
                event_created_at=event.created_at,
                event_source=event.source,
                event_message=event.message,
                warnings=warnings,
            )
        )

    for artifact in artifacts:
        artifact_path = str(artifact.path)
        if artifact_path in used_artifact_paths:
            continue
        artifact_payload, warnings = _read_json_payload(artifact.path)
        items.append(
            _normalize_verifier_report_item(
                task_key=task_key,
                event_payload={},
                artifact_payload=artifact_payload,
                artifact_path=artifact_path,
                artifact_created_at=artifact.created_at,
                event_created_at=None,
                event_source=None,
                event_message=None,
                warnings=warnings,
            )
        )

    return items


def _normalize_verifier_report_item(
    *,
    task_key: str,
    event_payload: dict[str, Any],
    artifact_payload: dict[str, Any] | None,
    artifact_path: str | None,
    artifact_created_at: str | None,
    event_created_at: str | None,
    event_source: str | None,
    event_message: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    payload = artifact_payload or {}
    resolved_artifact_path = _first_string(
        artifact_path,
        payload.get("artifact_path"),
        event_payload.get("artifact_path"),
    )
    return {
        "task_key": task_key,
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
        "confirmation_artifact_path": _first_string(
            event_payload.get("confirmation_artifact_path"),
            payload.get("confirmation_artifact_path"),
        ),
        "proposal_artifact_path": _first_string(
            event_payload.get("proposal_artifact_path"),
            payload.get("proposal_artifact_path"),
        ),
        "artifact_path": resolved_artifact_path,
        "event_created_at": event_created_at,
        "artifact_created_at": artifact_created_at,
        "event_source": event_source,
        "event_message": event_message,
        "readback_warnings": list(dict.fromkeys(warnings)),
    }


def _filter_verifier_report_items(
    items: list[dict[str, Any]],
    request: IntakeRunnerHandoffFromVerifierReportRequest,
) -> list[dict[str, Any]]:
    expected_artifact_path = (
        str(request.verifier_report_artifact_path)
        if request.verifier_report_artifact_path is not None
        else None
    )
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("verifier_report_id") != request.verifier_report_id:
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
        if request.item_hash is not None and item.get("item_hash") != request.item_hash:
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


def _copy_selected_verifier_report(
    selected: dict[str, Any],
    verifier_view: dict[str, Any],
) -> None:
    verifier_view["verifier_report_id"] = selected.get("verifier_report_id")
    verifier_view["confirmation_id"] = selected.get("confirmation_id")
    verifier_view["proposal_hash"] = selected.get("proposal_hash")
    verifier_view["proposal_item_id"] = selected.get("proposal_item_id")
    verifier_view["item_hash"] = selected.get("item_hash")
    verifier_view["recommended_command_kind"] = selected.get(
        "recommended_command_kind"
    )
    verifier_view["verifier_report_artifact_path"] = selected.get("artifact_path")
    verifier_view["confirmation_artifact_path"] = selected.get(
        "confirmation_artifact_path"
    )
    verifier_view["proposal_artifact_path"] = selected.get("proposal_artifact_path")


def _verify_verifier_report_payload(
    *,
    payload: dict[str, Any],
    selected: dict[str, Any],
    request: IntakeRunnerHandoffFromVerifierReportRequest,
    verifier_view: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    if payload.get("verification_passed") is True:
        checks["verifier_report_passed"] = True
    else:
        reasons.append(REASON_VERIFIER_REPORT_FAILED)

    for key in _VERIFIER_REQUIRED_BINDING_KEYS:
        value = _string_value(payload.get(key))
        if value is not None:
            verifier_view[key] = value

    artifact_path = _string_value(payload.get("artifact_path"))
    if artifact_path is not None:
        verifier_view["verifier_report_artifact_path"] = artifact_path

    selected_matches_payload = all(
        _string_value(selected.get(key)) == _string_value(payload.get(key))
        for key in (
            "verifier_report_id",
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
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
        for key in _VERIFIER_REQUIRED_BINDING_KEYS
    )
    artifact_path_matches = (
        request.verifier_report_artifact_path is None
        or artifact_path == str(request.verifier_report_artifact_path)
    )
    if (
        selected_matches_payload
        and request_matches_payload
        and required_bindings_present
        and artifact_path_matches
        and _verifier_safety_flags_valid(payload)
    ):
        checks["verifier_report_binding_matches"] = True
    else:
        reasons.append(REASON_VERIFIER_REPORT_BINDING_MISMATCH)


def _verify_confirmation_payload(
    *,
    payload: dict[str, Any],
    verifier: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    expected_artifact_path = _string_value(verifier.get("confirmation_artifact_path"))
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
    artifact_path_matches = (
        expected_artifact_path is not None
        and _string_value(payload.get("artifact_path")) == expected_artifact_path
    )
    if bindings_match and artifact_path_matches:
        checks["confirmation_binding_matches"] = True
    else:
        reasons.append(REASON_CONFIRMATION_BINDING_MISMATCH)


def _verify_proposal_payload(
    *,
    payload: dict[str, Any],
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


def _load_verifier_report_artifact(
    artifact_path: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    return _load_json_object(
        artifact_path,
        path_missing_reason=REASON_VERIFIER_REPORT_ARTIFACT_PATH_MISSING,
        file_missing_reason=REASON_VERIFIER_REPORT_ARTIFACT_FILE_MISSING,
        json_malformed_reason=REASON_VERIFIER_REPORT_ARTIFACT_JSON_MALFORMED,
        json_not_object_reason=REASON_VERIFIER_REPORT_ARTIFACT_JSON_NOT_OBJECT,
    )


def _load_confirmation_artifact(
    artifact_path: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    return _load_json_object(
        artifact_path,
        path_missing_reason=REASON_CONFIRMATION_ARTIFACT_PATH_MISSING,
        file_missing_reason=REASON_CONFIRMATION_ARTIFACT_FILE_MISSING,
        json_malformed_reason=REASON_CONFIRMATION_ARTIFACT_JSON_MALFORMED,
        json_not_object_reason=REASON_CONFIRMATION_ARTIFACT_JSON_NOT_OBJECT,
    )


def _load_proposal_artifact(
    artifact_path: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    return _load_json_object(
        artifact_path,
        path_missing_reason=REASON_PROPOSAL_ARTIFACT_PATH_MISSING,
        file_missing_reason=REASON_PROPOSAL_ARTIFACT_FILE_MISSING,
        json_malformed_reason=REASON_PROPOSAL_ARTIFACT_JSON_MALFORMED,
        json_not_object_reason=REASON_PROPOSAL_ARTIFACT_JSON_NOT_OBJECT,
    )


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
    except FileNotFoundError:
        return None, [file_missing_reason]
    except OSError:
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


def _find_proposal_item(
    payload: dict[str, Any],
    proposal_item_id: str | None,
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
    hash_report: dict[str, Any],
    proposal_item_id: str | None,
) -> dict[str, Any] | None:
    if not proposal_item_id:
        return None
    for entry in hash_report.get("items") or []:
        if isinstance(entry, dict) and entry.get("proposal_item_id") == proposal_item_id:
            return entry
    return None


def _resolve_expected_status(proposal_item: dict[str, Any] | None) -> str | None:
    if proposal_item is None:
        return None
    expected = _string_value(proposal_item.get("expected_status"))
    if expected:
        return expected
    return _string_value(proposal_item.get("status"))


def _verifier_safety_flags_valid(payload: dict[str, Any]) -> bool:
    if payload.get("not_execution_permission") is not True:
        return False
    if payload.get("not_runtime") is not True:
        return False
    if payload.get("requires_next_gate") is not True:
        return False
    if "not_handoff" in payload and payload.get("not_handoff") is not True:
        return False

    safety = payload.get("safety")
    if not isinstance(safety, dict):
        return False
    if safety.get("not_execution_permission") is not True:
        return False
    if safety.get("not_runtime") is not True:
        return False
    if safety.get("requires_next_gate") is not True:
        return False
    if "not_handoff" in safety and safety.get("not_handoff") is not True:
        return False
    return True


def _count_duplicate_handoffs(
    store: TaskMirrorStore,
    task_key: str,
    *,
    verifier_report_id: str,
    confirmation_id: str | None,
    proposal_hash: str | None,
    proposal_item_id: str | None,
    item_hash: str | None,
) -> int:
    if not (
        verifier_report_id
        and confirmation_id
        and proposal_hash
        and proposal_item_id
        and item_hash
    ):
        return 0

    count = 0
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type != HANDOFF_ARTIFACT_TYPE:
            continue
        payload, _ = _read_json_payload(artifact.path)
        if _matches_handoff_binding(
            payload,
            verifier_report_id=verifier_report_id,
            confirmation_id=confirmation_id,
            proposal_hash=proposal_hash,
            proposal_item_id=proposal_item_id,
            item_hash=item_hash,
        ):
            count += 1

    for event in store.list_task_events(task_key):
        if event.event_type != HANDOFF_EVENT_TYPE:
            continue
        payload, _ = _parse_event_payload(event.payload_json)
        if _matches_handoff_binding(
            payload,
            verifier_report_id=verifier_report_id,
            confirmation_id=confirmation_id,
            proposal_hash=proposal_hash,
            proposal_item_id=proposal_item_id,
            item_hash=item_hash,
        ):
            count += 1
    return count


def _matches_handoff_binding(
    payload: dict[str, Any] | None,
    *,
    verifier_report_id: str,
    confirmation_id: str,
    proposal_hash: str,
    proposal_item_id: str,
    item_hash: str,
) -> bool:
    if not payload:
        return False
    return (
        payload.get("verifier_report_id") == verifier_report_id
        and payload.get("confirmation_id") == confirmation_id
        and payload.get("proposal_hash") == proposal_hash
        and payload.get("proposal_item_id") == proposal_item_id
        and payload.get("item_hash") == item_hash
    )


def _build_handoff_payload(
    request: IntakeRunnerHandoffFromVerifierReportRequest,
    *,
    binding: dict[str, Any],
    mode: str,
    handoff_created: bool,
) -> dict[str, Any]:
    verifier = dict(binding.get("verifier_report") or {})
    handoff_id = _make_handoff_id(
        verifier_report_id=str(verifier["verifier_report_id"]),
        confirmation_id=str(verifier["confirmation_id"]),
        proposal_item_id=str(verifier["proposal_item_id"]),
        item_hash=str(verifier["item_hash"]),
    )
    artifact_path = (
        request.artifact_root
        / "intake_runner_handoffs"
        / handoff_id
        / "intake_runner_handoff.json"
    )
    safety = _safety(handoff_created=handoff_created)

    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "created_at": utc_now_iso(),
        "source": HANDOFF_SOURCE,
        "status": "created" if handoff_created else "dry_run",
        "mode": mode,
        "task_key": request.task_key,
        "verifier_report_id": verifier.get("verifier_report_id"),
        "confirmation_id": verifier.get("confirmation_id"),
        "proposal_hash": verifier.get("proposal_hash"),
        "proposal_item_id": verifier.get("proposal_item_id"),
        "item_hash": verifier.get("item_hash"),
        "recommended_command_kind": verifier.get("recommended_command_kind"),
        "verifier_report_artifact_path": verifier.get(
            "verifier_report_artifact_path"
        ),
        "confirmation_artifact_path": verifier.get("confirmation_artifact_path"),
        "proposal_artifact_path": verifier.get("proposal_artifact_path"),
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path),
        "operator": request.operator,
        "operator_note": request.operator_note,
        "handoff_allowed": True,
        "binding_summary": _binding_summary(binding),
        "reasons": list(binding.get("reasons") or []),
        "warnings": list(binding.get("warnings") or []),
        "checks": dict(binding.get("checks") or {}),
        "safety": safety,
        "not_execution_permission": True,
        "not_runtime": True,
        "approved_task_runner_called": False,
        "requires_runtime_preflight": True,
        "requires_next_gate": True,
    }


def _binding_summary(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": binding.get("schema_version"),
        "mode": binding.get("mode"),
        "handoff_allowed": binding.get("handoff_allowed"),
        "eligible_for_handoff": binding.get("eligible_for_handoff"),
        "task_key": binding.get("task_key"),
        "verifier_report": dict(binding.get("verifier_report") or {}),
        "current": dict(binding.get("current") or {}),
    }


def _event_payload(handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": HANDOFF_EVENT_TYPE,
        "handoff_id": handoff.get("handoff_id"),
        "verifier_report_id": handoff.get("verifier_report_id"),
        "confirmation_id": handoff.get("confirmation_id"),
        "proposal_hash": handoff.get("proposal_hash"),
        "proposal_item_id": handoff.get("proposal_item_id"),
        "item_hash": handoff.get("item_hash"),
        "task_key": handoff.get("task_key"),
        "recommended_command_kind": handoff.get("recommended_command_kind"),
        "verifier_report_artifact_path": handoff.get(
            "verifier_report_artifact_path"
        ),
        "confirmation_artifact_path": handoff.get("confirmation_artifact_path"),
        "proposal_artifact_path": handoff.get("proposal_artifact_path"),
        "artifact_path": handoff.get("artifact_path"),
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "handoff_allowed": True,
        "not_execution_permission": True,
        "not_runtime": True,
        "approved_task_runner_called": False,
        "requires_runtime_preflight": True,
        "requires_next_gate": True,
    }


def _safety(
    *,
    handoff_created: bool,
    read_only: bool | None = None,
) -> dict[str, bool]:
    safety = dict(HANDOFF_SAFETY_FLAGS)
    safety["handoff_created"] = handoff_created
    if read_only is not None:
        safety["read_only"] = read_only
    return safety


def _mode(request: IntakeRunnerHandoffFromVerifierReportRequest) -> str:
    return "dry_run" if request.dry_run else "confirmed"


def _make_handoff_id(
    *,
    verifier_report_id: str,
    confirmation_id: str,
    proposal_item_id: str,
    item_hash: str,
) -> str:
    timestamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "")
    digest = hashlib.sha256()
    digest.update(verifier_report_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(confirmation_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(proposal_item_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(item_hash.encode("utf-8"))
    digest.update(b"|")
    digest.update(uuid4().hex.encode("utf-8"))
    return f"handoff-{timestamp}-{digest.hexdigest()[:12]}"


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
    "HANDOFF_ARTIFACT_TYPE",
    "HANDOFF_EVENT_TYPE",
    "HANDOFF_SAFETY_FLAGS",
    "HANDOFF_SCHEMA_VERSION",
    "HANDOFF_SOURCE",
    "VERIFIER_REPORT_CONSUMED_EVENT_TYPE",
    "IntakeRunnerHandoffFromVerifierReportError",
    "IntakeRunnerHandoffFromVerifierReportRequest",
    "check_intake_runner_handoff_binding",
    "create_intake_runner_handoff_from_verifier_report",
]
