"""Minimal scheduler confirmation verifier report path.

Level 4A converts an already recorded ``scheduler_confirmation`` into
auditable verifier report evidence after an explicit operator command.

The binding helper is read-only. Report creation is dry-run by default
and writes only a ``scheduler_confirmation_verifier_report`` artifact
and a matching ``scheduler_confirmation_verifier_report_created`` event
when explicitly confirmed.

A scheduler confirmation verifier report is not execution permission,
not handoff creation, and not runtime execution. This module does not
update task status, invoke any executor, call validators, mutate GitHub,
approve, merge, run cleanup, start background workers, or run a scheduler
loop. It does not call the approved task runner and does not touch
Mission Control.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow.models import utc_now_iso
from agent_taskflow.scheduler_confirmation_readback import (
    list_task_scheduler_confirmation_readbacks,
)
from agent_taskflow.scheduler_proposals import verify_proposal_hashes
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


VERIFIER_REPORT_SCHEMA_VERSION = "scheduler_confirmation_verifier_report.v1"
VERIFIER_REPORT_SOURCE = "scheduler_confirmation_verifier_report"
VERIFIER_REPORT_ARTIFACT_TYPE = "scheduler_confirmation_verifier_report"
VERIFIER_REPORT_EVENT_TYPE = "scheduler_confirmation_verifier_report_created"
CONFIRMATION_CONSUMED_EVENT_TYPE = "scheduler_confirmation_consumed"

VERIFIER_REPORT_SAFETY_FLAGS: dict[str, bool] = {
    "verifier_report_created": False,
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
    "not_handoff": True,
    "not_runtime": True,
    "requires_next_gate": True,
}

REASON_TASK_MISSING = "task_missing"
REASON_CONFIRMATION_ITEM_NOT_FOUND = "confirmation_item_not_found"
REASON_CONFIRMATION_ITEM_AMBIGUOUS = "confirmation_item_ambiguous"
REASON_CONFIRMATION_ARTIFACT_PATH_MISSING = "confirmation_artifact_path_missing"
REASON_CONFIRMATION_ARTIFACT_FILE_MISSING = "confirmation_artifact_file_missing"
REASON_CONFIRMATION_ARTIFACT_JSON_MALFORMED = (
    "confirmation_artifact_json_malformed"
)
REASON_CONFIRMATION_ARTIFACT_JSON_NOT_OBJECT = (
    "confirmation_artifact_json_not_object"
)
REASON_CONFIRMATION_ID_MISMATCH = "confirmation_id_mismatch"
REASON_CONFIRMATION_BINDING_MISMATCH = "confirmation_binding_mismatch"
REASON_CONFIRMATION_SAFETY_FLAGS_INVALID = "confirmation_safety_flags_invalid"
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
REASON_DUPLICATE_ACTIVE_VERIFIER_REPORT = "duplicate_active_verifier_report"
REASON_CONFIRMATION_ALREADY_CONSUMED = "scheduler_confirmation_already_consumed"

_REQUIRED_CHECKS: tuple[str, ...] = (
    "confirmation_exists",
    "confirmation_artifact_exists",
    "confirmation_id_matches",
    "confirmation_binding_matches_readback",
    "confirmation_safety_flags_valid",
    "proposal_artifact_exists",
    "proposal_hash_matches_artifact",
    "proposal_item_id_exists",
    "item_hash_matches_selected_item",
    "task_still_exists",
    "task_status_matches_expected",
    "recommended_command_kind_matches",
    "duplicate_verifier_report_absent",
    "scheduler_confirmation_not_consumed",
)

_CONFIRMATION_REQUIRED_FLAGS: tuple[str, ...] = (
    "not_execution_permission",
    "not_verifier_report",
    "not_handoff",
    "not_runtime",
    "requires_next_gate",
)


class SchedulerConfirmationVerifierReportError(RuntimeError):
    """Raised when verifier report creation cannot proceed safely."""


@dataclass(frozen=True)
class SchedulerConfirmationVerifierReportRequest:
    """Inputs to verifier report binding and creation."""

    db_path: Path
    artifact_root: Path
    task_key: str
    confirmation_id: str
    proposal_hash: str | None = None
    proposal_item_id: str | None = None
    item_hash: str | None = None
    recommended_command_kind: str | None = None
    confirmation_artifact_path: Path | None = None
    dry_run: bool = True
    confirm_create_verifier_report: bool = False
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

        confirmation_id = (self.confirmation_id or "").strip()
        if not confirmation_id:
            raise ValueError("confirmation_id must not be empty")
        object.__setattr__(self, "confirmation_id", confirmation_id)

        for field_name in (
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

        if self.confirmation_artifact_path is not None:
            object.__setattr__(
                self,
                "confirmation_artifact_path",
                Path(self.confirmation_artifact_path).expanduser(),
            )


def check_scheduler_confirmation_verifier_binding(
    request: SchedulerConfirmationVerifierReportRequest,
) -> dict[str, Any]:
    """Return a read-only verifier binding report for one confirmation."""

    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {name: False for name in _REQUIRED_CHECKS}

    confirmation_view: dict[str, Any] = {
        "confirmation_id": request.confirmation_id,
        "proposal_hash": None,
        "proposal_item_id": None,
        "item_hash": None,
        "recommended_command_kind": None,
        "confirmation_artifact_path": None,
        "proposal_artifact_path": None,
    }
    current_view: dict[str, Any] = {
        "task_exists": False,
        "task_status": None,
        "expected_status": None,
        "duplicate_verifier_report_count": 0,
        "scheduler_confirmation_consumed_count": 0,
    }

    store = TaskMirrorStore(request.db_path)
    task_record = store.get_task(request.task_key)
    if task_record is None:
        reasons.append(REASON_TASK_MISSING)
    else:
        checks["task_still_exists"] = True
        current_view["task_exists"] = True
        current_view["task_status"] = task_record.status

    readback = list_task_scheduler_confirmation_readbacks(
        store,
        request.task_key,
    )
    matches = _filter_confirmation_readback(readback.get("items") or [], request)

    selected: dict[str, Any] | None = None
    confirmation_payload: dict[str, Any] | None = None
    proposal_payload: dict[str, Any] | None = None
    proposal_item: dict[str, Any] | None = None
    hash_report: dict[str, Any] | None = None

    if not matches:
        reasons.append(REASON_CONFIRMATION_ITEM_NOT_FOUND)
    elif len(matches) > 1:
        reasons.append(REASON_CONFIRMATION_ITEM_AMBIGUOUS)
    else:
        selected = matches[0]
        checks["confirmation_exists"] = True
        warnings.extend(selected.get("readback_warnings") or [])
        _copy_selected_confirmation(selected, confirmation_view)

        confirmation_payload, load_reasons = _load_confirmation_artifact(
            selected.get("artifact_path")
        )
        reasons.extend(load_reasons)
        if confirmation_payload is not None:
            checks["confirmation_artifact_exists"] = True
            _verify_confirmation_payload(
                payload=confirmation_payload,
                selected=selected,
                request=request,
                confirmation_view=confirmation_view,
                checks=checks,
                reasons=reasons,
            )

            proposal_payload, proposal_reasons = _load_proposal_artifact(
                confirmation_payload.get("proposal_artifact_path")
            )
            reasons.extend(proposal_reasons)
            if proposal_payload is not None:
                checks["proposal_artifact_exists"] = True
                hash_report = verify_proposal_hashes(proposal_payload)
                proposal_item = _find_proposal_item(
                    proposal_payload,
                    _string_value(selected.get("proposal_item_id")),
                )
                _verify_proposal_payload(
                    payload=proposal_payload,
                    selected=selected,
                    proposal_item=proposal_item,
                    hash_report=hash_report,
                    checks=checks,
                    reasons=reasons,
                )
                _verify_current_task_and_kind(
                    selected=selected,
                    confirmation_payload=confirmation_payload,
                    proposal_item=proposal_item,
                    task_record=task_record,
                    current_view=current_view,
                    checks=checks,
                    reasons=reasons,
                )

    duplicate_count = _count_duplicate_verifier_reports(
        store,
        request.task_key,
        confirmation_id=request.confirmation_id,
        proposal_hash=_string_value(confirmation_view.get("proposal_hash"))
        or request.proposal_hash,
        proposal_item_id=_string_value(confirmation_view.get("proposal_item_id"))
        or request.proposal_item_id,
        item_hash=_string_value(confirmation_view.get("item_hash"))
        or request.item_hash,
    )
    current_view["duplicate_verifier_report_count"] = duplicate_count
    if duplicate_count == 0:
        checks["duplicate_verifier_report_absent"] = True
    else:
        reasons.append(REASON_DUPLICATE_ACTIVE_VERIFIER_REPORT)

    consumed_events = store.list_lineage_consumption_events(
        request.task_key,
        CONFIRMATION_CONSUMED_EVENT_TYPE,
        consumed_artifact_type="scheduler_confirmation",
        consumed_artifact_path=_string_value(
            confirmation_view.get("confirmation_artifact_path")
        ),
        confirmation_id=request.confirmation_id,
        proposal_hash=_string_value(confirmation_view.get("proposal_hash"))
        or request.proposal_hash,
        proposal_item_id=_string_value(confirmation_view.get("proposal_item_id"))
        or request.proposal_item_id,
        item_hash=_string_value(confirmation_view.get("item_hash")) or request.item_hash,
    )
    current_view["scheduler_confirmation_consumed_count"] = len(consumed_events)
    if not consumed_events:
        checks["scheduler_confirmation_not_consumed"] = True
    else:
        reasons.append(REASON_CONFIRMATION_ALREADY_CONSUMED)

    unique_reasons = list(dict.fromkeys(reasons))
    unique_warnings = list(dict.fromkeys(warnings))
    verification_passed = not unique_reasons and all(
        checks[name] for name in _REQUIRED_CHECKS
    )

    return {
        "ok": True,
        "schema_version": VERIFIER_REPORT_SCHEMA_VERSION,
        "mode": "read_only",
        "task_key": request.task_key,
        "verification_passed": verification_passed,
        "eligible_for_report": verification_passed,
        "reasons": unique_reasons,
        "warnings": unique_warnings,
        "confirmation": confirmation_view,
        "current": current_view,
        "checks": checks,
        "safety": _safety(verifier_report_created=False, read_only=True),
    }


def create_scheduler_confirmation_verifier_report(
    request: SchedulerConfirmationVerifierReportRequest,
) -> dict[str, Any]:
    """Create verifier report evidence after an explicit confirmation."""

    if not request.dry_run and not request.confirm_create_verifier_report:
        raise SchedulerConfirmationVerifierReportError(
            "Non-dry-run scheduler confirmation verifier report creation "
            "requires confirm_create_verifier_report=True"
        )

    binding = check_scheduler_confirmation_verifier_binding(request)
    if not binding.get("verification_passed"):
        return {
            "ok": False,
            "schema_version": VERIFIER_REPORT_SCHEMA_VERSION,
            "source": VERIFIER_REPORT_SOURCE,
            "status": "not_verified",
            "mode": _mode(request),
            "verification_passed": False,
            "reasons": list(binding.get("reasons") or []),
            "binding": binding,
            "safety": _safety(verifier_report_created=False),
        }

    verifier_report = _build_verifier_report_payload(
        request,
        binding=binding,
        mode=_mode(request),
        verifier_report_created=not request.dry_run,
    )

    if request.dry_run:
        return {
            "ok": True,
            "schema_version": VERIFIER_REPORT_SCHEMA_VERSION,
            "source": VERIFIER_REPORT_SOURCE,
            "status": "dry_run",
            "mode": "dry_run",
            "would_create_verifier_report": True,
            "verifier_report": verifier_report,
            "binding": binding,
            "safety": _safety(verifier_report_created=False),
        }

    artifact_path = Path(verifier_report["artifact_path"])
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(verifier_report, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    store = TaskMirrorStore(request.db_path)
    store.record_task_artifact(
        request.task_key,
        VERIFIER_REPORT_ARTIFACT_TYPE,
        artifact_path,
    )
    event_payload = _event_payload(verifier_report)
    store.record_task_event(
        request.task_key,
        VERIFIER_REPORT_EVENT_TYPE,
        VERIFIER_REPORT_SOURCE,
        message=(
            f"Scheduler confirmation verifier report "
            f"{verifier_report['verifier_report_id']} recorded "
            "(audit only; not execution permission)"
        ),
        payload=event_payload,
    )
    store.record_lineage_consumed(
        request.task_key,
        CONFIRMATION_CONSUMED_EVENT_TYPE,
        VERIFIER_REPORT_SOURCE,
        consumed_artifact_type="scheduler_confirmation",
        consumed_artifact_path=str(verifier_report["confirmation_artifact_path"]),
        consumer_artifact_type=VERIFIER_REPORT_ARTIFACT_TYPE,
        consumer_artifact_path=artifact_path,
        confirmation_id=str(verifier_report["confirmation_id"]),
        verifier_report_id=str(verifier_report["verifier_report_id"]),
        proposal_hash=str(verifier_report["proposal_hash"]),
        proposal_item_id=str(verifier_report["proposal_item_id"]),
        item_hash=str(verifier_report["item_hash"]),
    )

    return {
        "ok": True,
        "schema_version": VERIFIER_REPORT_SCHEMA_VERSION,
        "source": VERIFIER_REPORT_SOURCE,
        "status": "created",
        "mode": "confirmed",
        "verification_passed": True,
        "verifier_report": verifier_report,
        "binding": binding,
        "safety": _safety(verifier_report_created=True),
    }


def _filter_confirmation_readback(
    items: list[dict[str, Any]],
    request: SchedulerConfirmationVerifierReportRequest,
) -> list[dict[str, Any]]:
    expected_artifact_path = (
        str(request.confirmation_artifact_path)
        if request.confirmation_artifact_path is not None
        else None
    )
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("confirmation_id") != request.confirmation_id:
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
        if expected_artifact_path is not None and item.get("artifact_path") != expected_artifact_path:
            continue
        matches.append(item)
    return matches


def _copy_selected_confirmation(
    selected: dict[str, Any],
    confirmation_view: dict[str, Any],
) -> None:
    confirmation_view["confirmation_id"] = selected.get("confirmation_id")
    confirmation_view["proposal_hash"] = selected.get("proposal_hash")
    confirmation_view["proposal_item_id"] = selected.get("proposal_item_id")
    confirmation_view["item_hash"] = selected.get("item_hash")
    confirmation_view["recommended_command_kind"] = selected.get(
        "recommended_command_kind"
    )
    confirmation_view["confirmation_artifact_path"] = selected.get("artifact_path")
    confirmation_view["proposal_artifact_path"] = selected.get("proposal_artifact_path")


def _verify_confirmation_payload(
    *,
    payload: dict[str, Any],
    selected: dict[str, Any],
    request: SchedulerConfirmationVerifierReportRequest,
    confirmation_view: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    if _string_value(payload.get("confirmation_id")) == request.confirmation_id:
        checks["confirmation_id_matches"] = True
    else:
        reasons.append(REASON_CONFIRMATION_ID_MISMATCH)

    bound_keys = ("proposal_hash", "proposal_item_id", "item_hash")
    if all(
        _string_value(payload.get(key)) == _string_value(selected.get(key))
        for key in bound_keys
    ):
        checks["confirmation_binding_matches_readback"] = True
    else:
        reasons.append(REASON_CONFIRMATION_BINDING_MISMATCH)

    if all(payload.get(flag) is True for flag in _CONFIRMATION_REQUIRED_FLAGS):
        checks["confirmation_safety_flags_valid"] = True
    else:
        reasons.append(REASON_CONFIRMATION_SAFETY_FLAGS_INVALID)

    proposal_path = _string_value(payload.get("proposal_artifact_path"))
    if proposal_path:
        confirmation_view["proposal_artifact_path"] = proposal_path


def _verify_proposal_payload(
    *,
    payload: dict[str, Any],
    selected: dict[str, Any],
    proposal_item: dict[str, Any] | None,
    hash_report: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    selected_proposal_hash = _string_value(selected.get("proposal_hash"))
    if (
        selected_proposal_hash
        and hash_report.get("proposal_hash_valid") is True
        and hash_report.get("actual_proposal_hash") == selected_proposal_hash
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
        _string_value(selected.get("proposal_item_id")),
    )
    selected_item_hash = _string_value(selected.get("item_hash"))
    if (
        selected_item_hash
        and item_report is not None
        and item_report.get("item_hash_valid") is True
        and item_report.get("actual_item_hash") == selected_item_hash
        and _string_value(proposal_item.get("item_hash")) == selected_item_hash
    ):
        checks["item_hash_matches_selected_item"] = True
    else:
        reasons.append(REASON_ITEM_HASH_MISMATCH)


def _verify_current_task_and_kind(
    *,
    selected: dict[str, Any],
    confirmation_payload: dict[str, Any],
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

    selected_kind = _string_value(selected.get("recommended_command_kind"))
    confirmation_kind = _string_value(confirmation_payload.get("recommended_command_kind"))
    proposal_kind = (
        _string_value(proposal_item.get("recommended_command_kind"))
        if proposal_item is not None
        else None
    )
    if selected_kind and confirmation_kind == selected_kind and proposal_kind == selected_kind:
        checks["recommended_command_kind_matches"] = True
    else:
        reasons.append(REASON_RECOMMENDED_COMMAND_KIND_MISMATCH)


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


def _count_duplicate_verifier_reports(
    store: TaskMirrorStore,
    task_key: str,
    *,
    confirmation_id: str,
    proposal_hash: str | None,
    proposal_item_id: str | None,
    item_hash: str | None,
) -> int:
    if not (confirmation_id and proposal_hash and proposal_item_id and item_hash):
        return 0

    count = 0
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type != VERIFIER_REPORT_ARTIFACT_TYPE:
            continue
        payload, _ = _load_json_object(
            artifact.path,
            path_missing_reason="",
            file_missing_reason="",
            json_malformed_reason="",
            json_not_object_reason="",
        )
        if _matches_verifier_report_binding(
            payload,
            confirmation_id=confirmation_id,
            proposal_hash=proposal_hash,
            proposal_item_id=proposal_item_id,
            item_hash=item_hash,
        ):
            count += 1

    for event in store.list_task_events(task_key):
        if event.event_type != VERIFIER_REPORT_EVENT_TYPE:
            continue
        payload = _parse_event_payload(event.payload_json)
        if _matches_verifier_report_binding(
            payload,
            confirmation_id=confirmation_id,
            proposal_hash=proposal_hash,
            proposal_item_id=proposal_item_id,
            item_hash=item_hash,
        ):
            count += 1
    return count


def _matches_verifier_report_binding(
    payload: dict[str, Any] | None,
    *,
    confirmation_id: str,
    proposal_hash: str,
    proposal_item_id: str,
    item_hash: str,
) -> bool:
    if not payload:
        return False
    return (
        payload.get("confirmation_id") == confirmation_id
        and payload.get("proposal_hash") == proposal_hash
        and payload.get("proposal_item_id") == proposal_item_id
        and payload.get("item_hash") == item_hash
    )


def _parse_event_payload(payload_json: str | None) -> dict[str, Any] | None:
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _build_verifier_report_payload(
    request: SchedulerConfirmationVerifierReportRequest,
    *,
    binding: dict[str, Any],
    mode: str,
    verifier_report_created: bool,
) -> dict[str, Any]:
    confirmation = dict(binding.get("confirmation") or {})
    verifier_report_id = _make_verifier_report_id(
        confirmation_id=str(confirmation["confirmation_id"]),
        proposal_item_id=str(confirmation["proposal_item_id"]),
        item_hash=str(confirmation["item_hash"]),
    )
    artifact_path = (
        request.artifact_root
        / "scheduler_confirmation_verifier_reports"
        / verifier_report_id
        / "scheduler_confirmation_verifier_report.json"
    )
    safety = _safety(verifier_report_created=verifier_report_created)

    return {
        "schema_version": VERIFIER_REPORT_SCHEMA_VERSION,
        "verifier_report_id": verifier_report_id,
        "created_at": utc_now_iso(),
        "source": VERIFIER_REPORT_SOURCE,
        "mode": mode,
        "task_key": request.task_key,
        "confirmation_id": confirmation.get("confirmation_id"),
        "proposal_hash": confirmation.get("proposal_hash"),
        "proposal_item_id": confirmation.get("proposal_item_id"),
        "item_hash": confirmation.get("item_hash"),
        "recommended_command_kind": confirmation.get("recommended_command_kind"),
        "confirmation_artifact_path": confirmation.get("confirmation_artifact_path"),
        "proposal_artifact_path": confirmation.get("proposal_artifact_path"),
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path),
        "operator": request.operator,
        "operator_note": request.operator_note,
        "verification_passed": True,
        "binding_summary": _binding_summary(binding),
        "reasons": list(binding.get("reasons") or []),
        "warnings": list(binding.get("warnings") or []),
        "checks": dict(binding.get("checks") or {}),
        "safety": safety,
        "not_execution_permission": True,
        "not_handoff": True,
        "not_runtime": True,
        "requires_next_gate": True,
    }


def _binding_summary(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": binding.get("schema_version"),
        "mode": binding.get("mode"),
        "verification_passed": binding.get("verification_passed"),
        "eligible_for_report": binding.get("eligible_for_report"),
        "task_key": binding.get("task_key"),
        "confirmation": dict(binding.get("confirmation") or {}),
        "current": dict(binding.get("current") or {}),
    }


def _event_payload(verifier_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": VERIFIER_REPORT_EVENT_TYPE,
        "verifier_report_id": verifier_report.get("verifier_report_id"),
        "confirmation_id": verifier_report.get("confirmation_id"),
        "proposal_hash": verifier_report.get("proposal_hash"),
        "proposal_item_id": verifier_report.get("proposal_item_id"),
        "item_hash": verifier_report.get("item_hash"),
        "task_key": verifier_report.get("task_key"),
        "recommended_command_kind": verifier_report.get("recommended_command_kind"),
        "confirmation_artifact_path": verifier_report.get(
            "confirmation_artifact_path"
        ),
        "proposal_artifact_path": verifier_report.get("proposal_artifact_path"),
        "artifact_path": verifier_report.get("artifact_path"),
        "schema_version": VERIFIER_REPORT_SCHEMA_VERSION,
        "verification_passed": True,
        "not_execution_permission": True,
        "not_handoff": True,
        "not_runtime": True,
        "requires_next_gate": True,
    }


def _safety(
    *,
    verifier_report_created: bool,
    read_only: bool | None = None,
) -> dict[str, bool]:
    safety = dict(VERIFIER_REPORT_SAFETY_FLAGS)
    safety["verifier_report_created"] = verifier_report_created
    if read_only is not None:
        safety["read_only"] = read_only
    return safety


def _mode(request: SchedulerConfirmationVerifierReportRequest) -> str:
    return "dry_run" if request.dry_run else "confirmed"


def _make_verifier_report_id(
    *,
    confirmation_id: str,
    proposal_item_id: str,
    item_hash: str,
) -> str:
    timestamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "")
    digest = hashlib.sha256()
    digest.update(confirmation_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(proposal_item_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(item_hash.encode("utf-8"))
    digest.update(b"|")
    digest.update(uuid4().hex.encode("utf-8"))
    return f"verifier-report-{timestamp}-{digest.hexdigest()[:12]}"


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


__all__ = [
    "VERIFIER_REPORT_ARTIFACT_TYPE",
    "CONFIRMATION_CONSUMED_EVENT_TYPE",
    "VERIFIER_REPORT_EVENT_TYPE",
    "VERIFIER_REPORT_SAFETY_FLAGS",
    "VERIFIER_REPORT_SCHEMA_VERSION",
    "VERIFIER_REPORT_SOURCE",
    "SchedulerConfirmationVerifierReportError",
    "SchedulerConfirmationVerifierReportRequest",
    "check_scheduler_confirmation_verifier_binding",
    "create_scheduler_confirmation_verifier_report",
]
