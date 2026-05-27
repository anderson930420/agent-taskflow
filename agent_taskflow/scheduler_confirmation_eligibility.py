"""Read-only scheduler confirmation eligibility helper.

This module answers a single read-only question: would a previously
recorded scheduler_proposal item currently be eligible for future
explicit scheduler confirmation preparation?

It is read-only by design. It does not write artifacts, mutate the
local mirror DB, emit events, run any executor, run any validator,
invoke any task runner, mutate GitHub, approve, merge, perform cleanup,
or start a background worker. Mission Control remains read-only and is
not touched by this module.

Eligibility is computed by rereading scheduler_proposal evidence from
the local mirror and verifying that the selected proposal item still
matches the persisted artifact, that ``proposal_hash`` and the per-item
``item_hash`` verify, that the current task status still matches the
expected status, and that no prior ``scheduler_confirmation`` evidence
already binds the same proposal_hash + proposal_item_id + item_hash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.scheduler_proposal_readback import (
    list_task_scheduler_proposal_readbacks,
)
from agent_taskflow.scheduler_proposals import (
    verify_proposal_hashes,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


ELIGIBILITY_SCHEMA_VERSION = "scheduler_confirmation_eligibility.v1"
ELIGIBILITY_MODE = "read_only"

CONFIRMATION_ARTIFACT_TYPE = "scheduler_confirmation"
CONFIRMATION_EVENT_TYPE = "scheduler_confirmation_created"

ELIGIBILITY_SAFETY_FLAGS: dict[str, bool] = {
    "read_only": True,
    "confirmation_created": False,
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
    "not_verifier_report": True,
    "not_handoff": True,
    "not_runtime": True,
}


REASON_TASK_MISSING = "task_missing"
REASON_PROPOSAL_ITEM_NOT_FOUND = "proposal_item_not_found"
REASON_PROPOSAL_ITEM_AMBIGUOUS = "proposal_item_ambiguous"
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
REASON_DUPLICATE_ACTIVE_CONFIRMATION = "duplicate_active_confirmation"


_REQUIRED_CHECKS: tuple[str, ...] = (
    "proposal_exists",
    "proposal_artifact_exists",
    "proposal_hash_matches_artifact",
    "proposal_item_id_exists",
    "item_hash_matches_selected_item",
    "task_still_exists",
    "task_status_matches_expected",
    "recommended_command_kind_matches",
    "duplicate_active_confirmation_absent",
)


class SchedulerConfirmationEligibilityError(RuntimeError):
    """Raised when the eligibility helper cannot read state safely."""


@dataclass(frozen=True)
class SchedulerConfirmationEligibilityRequest:
    """Inputs to a read-only eligibility check."""

    db_path: Path
    task_key: str
    proposal_item_id: str
    proposal_hash: str | None = None
    proposal_id: str | None = None
    item_hash: str | None = None
    recommended_command_kind: str | None = None
    expected_status: str | None = None
    proposal_artifact_path: Path | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        proposal_item_id = (self.proposal_item_id or "").strip()
        if not proposal_item_id:
            raise ValueError("proposal_item_id must not be empty")
        object.__setattr__(self, "proposal_item_id", proposal_item_id)

        for field_name in (
            "proposal_hash",
            "proposal_id",
            "item_hash",
            "recommended_command_kind",
            "expected_status",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = value.strip()
            object.__setattr__(self, field_name, stripped or None)

        if self.proposal_artifact_path is not None:
            object.__setattr__(
                self,
                "proposal_artifact_path",
                Path(self.proposal_artifact_path).expanduser(),
            )


def check_scheduler_confirmation_eligibility(
    request: SchedulerConfirmationEligibilityRequest,
) -> dict[str, Any]:
    """Return a stable eligibility report for one scheduler_proposal item.

    The helper rereads proposal evidence from the local mirror and the
    backing artifact JSON. It never mutates the DB and never writes a
    confirmation artifact.
    """

    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {name: False for name in _REQUIRED_CHECKS}

    proposal_view: dict[str, Any] = {
        "proposal_id": None,
        "proposal_hash": None,
        "proposal_item_id": request.proposal_item_id,
        "item_hash": None,
        "recommended_command_kind": None,
        "proposal_artifact_path": None,
    }
    current_view: dict[str, Any] = {
        "task_exists": False,
        "task_status": None,
        "expected_status": request.expected_status,
        "duplicate_confirmation_count": 0,
    }

    store = TaskMirrorStore(request.db_path)
    task_record = store.get_task(request.task_key)
    if task_record is None:
        reasons.append(REASON_TASK_MISSING)
    else:
        checks["task_still_exists"] = True
        current_view["task_exists"] = True
        current_view["task_status"] = task_record.status

    readback = list_task_scheduler_proposal_readbacks(store, request.task_key)
    matches = _filter_readback(readback.get("items") or [], request)

    if not matches:
        reasons.append(REASON_PROPOSAL_ITEM_NOT_FOUND)
    elif len(matches) > 1:
        reasons.append(REASON_PROPOSAL_ITEM_AMBIGUOUS)

    selected = matches[0] if len(matches) == 1 else None
    if selected is not None:
        checks["proposal_exists"] = True
        warnings.extend(selected.get("readback_warnings") or [])
        proposal_view["proposal_id"] = selected.get("proposal_id")
        proposal_view["proposal_hash"] = selected.get("proposal_hash")
        proposal_view["item_hash"] = selected.get("item_hash")
        proposal_view["recommended_command_kind"] = selected.get(
            "recommended_command_kind"
        )
        proposal_view["proposal_artifact_path"] = selected.get("artifact_path")

        artifact_payload, artifact_reasons = _load_artifact_payload(
            selected.get("artifact_path")
        )
        reasons.extend(artifact_reasons)

        if artifact_payload is not None:
            checks["proposal_artifact_exists"] = True

            _verify_hashes(
                payload=artifact_payload,
                request=request,
                selected=selected,
                checks=checks,
                reasons=reasons,
            )

            _verify_status_and_kind(
                payload=artifact_payload,
                request=request,
                task_record=task_record,
                current_view=current_view,
                checks=checks,
                reasons=reasons,
            )

    duplicate_count = _count_duplicate_confirmations(
        store,
        request.task_key,
        proposal_hash=proposal_view["proposal_hash"],
        proposal_item_id=proposal_view["proposal_item_id"],
        item_hash=proposal_view["item_hash"],
    )
    current_view["duplicate_confirmation_count"] = duplicate_count
    if duplicate_count == 0:
        checks["duplicate_active_confirmation_absent"] = True
    else:
        reasons.append(REASON_DUPLICATE_ACTIVE_CONFIRMATION)

    eligible = not reasons and all(checks[name] for name in _REQUIRED_CHECKS)

    return {
        "ok": True,
        "schema_version": ELIGIBILITY_SCHEMA_VERSION,
        "mode": ELIGIBILITY_MODE,
        "task_key": request.task_key,
        "eligible": eligible,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "proposal": proposal_view,
        "current": current_view,
        "checks": checks,
        "safety": dict(ELIGIBILITY_SAFETY_FLAGS),
    }


def _filter_readback(
    items: list[dict[str, Any]],
    request: SchedulerConfirmationEligibilityRequest,
) -> list[dict[str, Any]]:
    expected_artifact_path = (
        str(request.proposal_artifact_path)
        if request.proposal_artifact_path is not None
        else None
    )
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("proposal_item_id") != request.proposal_item_id:
            continue
        if (
            request.proposal_hash is not None
            and item.get("proposal_hash") != request.proposal_hash
        ):
            continue
        if (
            request.proposal_id is not None
            and item.get("proposal_id") != request.proposal_id
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


def _verify_hashes(
    *,
    payload: dict[str, Any],
    request: SchedulerConfirmationEligibilityRequest,
    selected: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    hash_report = verify_proposal_hashes(payload)
    expected_proposal_hash = selected.get("proposal_hash")
    actual_proposal_hash = hash_report.get("actual_proposal_hash")

    proposal_hash_ok = (
        bool(expected_proposal_hash)
        and isinstance(actual_proposal_hash, str)
        and actual_proposal_hash == expected_proposal_hash
        and hash_report.get("proposal_hash_valid") is True
    )
    if proposal_hash_ok:
        checks["proposal_hash_matches_artifact"] = True
    else:
        reasons.append(REASON_PROPOSAL_HASH_MISMATCH)

    artifact_item = _find_artifact_item(payload, request.proposal_item_id)
    if artifact_item is None:
        reasons.append(REASON_PROPOSAL_ITEM_ID_MISSING_FROM_ARTIFACT)
        return

    checks["proposal_item_id_exists"] = True

    item_report = _item_hash_report(hash_report, request.proposal_item_id)
    actual_item_hash = artifact_item.get("item_hash")
    selected_item_hash = selected.get("item_hash")
    expected_item_hash = (
        request.item_hash if request.item_hash is not None else selected_item_hash
    )
    item_hash_valid = (
        item_report is not None and item_report.get("item_hash_valid") is True
    )
    item_hash_ok = (
        item_hash_valid
        and bool(expected_item_hash)
        and actual_item_hash == expected_item_hash
        and selected_item_hash == actual_item_hash
    )
    if item_hash_ok:
        checks["item_hash_matches_selected_item"] = True
    else:
        reasons.append(REASON_ITEM_HASH_MISMATCH)


def _verify_status_and_kind(
    *,
    payload: dict[str, Any],
    request: SchedulerConfirmationEligibilityRequest,
    task_record: Any,
    current_view: dict[str, Any],
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    artifact_item = _find_artifact_item(payload, request.proposal_item_id)

    if request.recommended_command_kind is None:
        checks["recommended_command_kind_matches"] = True
    elif artifact_item is None:
        reasons.append(REASON_RECOMMENDED_COMMAND_KIND_MISMATCH)
    elif (
        artifact_item.get("recommended_command_kind")
        == request.recommended_command_kind
    ):
        checks["recommended_command_kind_matches"] = True
    else:
        reasons.append(REASON_RECOMMENDED_COMMAND_KIND_MISMATCH)

    expected_status = _resolve_expected_status(request, artifact_item)
    current_view["expected_status"] = expected_status

    if task_record is None:
        return

    if expected_status is None:
        checks["task_status_matches_expected"] = True
        return

    if task_record.status == expected_status:
        checks["task_status_matches_expected"] = True
    else:
        reasons.append(REASON_TASK_STATUS_MISMATCH)


def _resolve_expected_status(
    request: SchedulerConfirmationEligibilityRequest,
    artifact_item: dict[str, Any] | None,
) -> str | None:
    if request.expected_status is not None:
        return request.expected_status
    if artifact_item is None:
        return None
    expected = artifact_item.get("expected_status")
    if isinstance(expected, str) and expected.strip():
        return expected.strip()
    status = artifact_item.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return None


def _load_artifact_payload(
    artifact_path: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not artifact_path:
        return None, [REASON_PROPOSAL_ARTIFACT_PATH_MISSING]

    path = Path(artifact_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [REASON_PROPOSAL_ARTIFACT_FILE_MISSING]
    except OSError:
        return None, [REASON_PROPOSAL_ARTIFACT_FILE_MISSING]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, [REASON_PROPOSAL_ARTIFACT_JSON_MALFORMED]

    if not isinstance(payload, dict):
        return None, [REASON_PROPOSAL_ARTIFACT_JSON_NOT_OBJECT]
    return payload, []


def _find_artifact_item(
    payload: dict[str, Any],
    proposal_item_id: str,
) -> dict[str, Any] | None:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("proposal_item_id") == proposal_item_id:
            return item
    return None


def _item_hash_report(
    hash_report: dict[str, Any],
    proposal_item_id: str,
) -> dict[str, Any] | None:
    for entry in hash_report.get("items") or []:
        if isinstance(entry, dict) and entry.get("proposal_item_id") == proposal_item_id:
            return entry
    return None


def _count_duplicate_confirmations(
    store: TaskMirrorStore,
    task_key: str,
    *,
    proposal_hash: str | None,
    proposal_item_id: str | None,
    item_hash: str | None,
) -> int:
    if not (proposal_hash and proposal_item_id and item_hash):
        return 0

    count = 0
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type != CONFIRMATION_ARTIFACT_TYPE:
            continue
        payload = _read_confirmation_artifact(artifact.path)
        if _matches_binding(payload, proposal_hash, proposal_item_id, item_hash):
            count += 1

    for evt in store.list_task_events(task_key):
        if evt.event_type != CONFIRMATION_EVENT_TYPE:
            continue
        payload = _parse_event_payload(evt.payload_json)
        if _matches_binding(payload, proposal_hash, proposal_item_id, item_hash):
            count += 1

    return count


def _matches_binding(
    payload: dict[str, Any] | None,
    proposal_hash: str,
    proposal_item_id: str,
    item_hash: str,
) -> bool:
    if not payload:
        return False
    return (
        payload.get("proposal_hash") == proposal_hash
        and payload.get("proposal_item_id") == proposal_item_id
        and payload.get("item_hash") == item_hash
    )


def _read_confirmation_artifact(path: Path) -> dict[str, Any] | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


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


__all__ = [
    "CONFIRMATION_ARTIFACT_TYPE",
    "CONFIRMATION_EVENT_TYPE",
    "ELIGIBILITY_MODE",
    "ELIGIBILITY_SAFETY_FLAGS",
    "ELIGIBILITY_SCHEMA_VERSION",
    "SchedulerConfirmationEligibilityError",
    "SchedulerConfirmationEligibilityRequest",
    "check_scheduler_confirmation_eligibility",
]
