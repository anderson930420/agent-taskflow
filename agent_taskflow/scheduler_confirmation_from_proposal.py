"""Explicit scheduler confirmation creation from a stored proposal.

This module is the Phase K2 bridge from a recorded ``scheduler_proposal``
item to a recorded ``scheduler_confirmation`` artifact/event. It is
intentionally thin: the K1 read-only eligibility helper
(:mod:`agent_taskflow.scheduler_confirmation_eligibility`) is the sole
authority on whether a previously recorded proposal item is still
bindable, and this module only converts an *eligible* item into
``scheduler_confirmation`` evidence after an explicit operator command.

A scheduler confirmation produced here is NOT execution permission.
It is NOT a verifier report, NOT a handoff, and NOT runtime execution.
It is auditable evidence used by the next gate.

This module does NOT update task status, invoke any executor, call any
validator, mutate GitHub, approve, merge, run cleanup, start a
background worker, or run a scheduler loop. It does NOT call the
approved task runner, create verifier reports, or create handoffs.
Mission Control is not touched.

The legacy :mod:`agent_taskflow.scheduler_confirmations` module (which
records a different multi-item pre-approval contract) is intentionally
left untouched and is not imported here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import utc_now_iso
from agent_taskflow.scheduler_confirmation_eligibility import (
    SchedulerConfirmationEligibilityRequest,
    check_scheduler_confirmation_eligibility,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION = "scheduler_confirmation_from_proposal.v1"
CONFIRMATION_FROM_PROPOSAL_SOURCE = "scheduler_confirmation_from_proposal"

CONFIRMATION_ARTIFACT_TYPE = "scheduler_confirmation"
CONFIRMATION_EVENT_TYPE = "scheduler_confirmation_created"


CONFIRMATION_SAFETY_FLAGS: dict[str, bool] = {
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
    "requires_next_gate": True,
}


class SchedulerConfirmationFromProposalError(RuntimeError):
    """Raised when scheduler confirmation creation cannot proceed safely."""


@dataclass(frozen=True)
class SchedulerConfirmationFromProposalRequest:
    """Inputs to explicit scheduler confirmation creation from a proposal."""

    db_path: Path
    artifact_root: Path
    task_key: str
    proposal_item_id: str
    proposal_hash: str | None = None
    proposal_id: str | None = None
    item_hash: str | None = None
    recommended_command_kind: str | None = None
    expected_status: str | None = None
    proposal_artifact_path: Path | None = None
    dry_run: bool = True
    confirm_create_confirmation: bool = False
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
            "operator",
            "operator_note",
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


def create_scheduler_confirmation_from_proposal(
    request: SchedulerConfirmationFromProposalRequest,
) -> dict[str, Any]:
    """Convert an eligible proposal item into a scheduler_confirmation.

    Dry-run by default. Recording is gated by ``dry_run=False`` AND
    ``confirm_create_confirmation=True``; either alone is rejected.
    The confirmation artifact/event is auditable evidence only; it is
    not execution permission, not a verifier report, not a handoff, and
    not runtime execution.
    """

    if not request.dry_run and not request.confirm_create_confirmation:
        raise SchedulerConfirmationFromProposalError(
            "Non-dry-run scheduler confirmation creation requires "
            "confirm_create_confirmation=True"
        )

    eligibility = check_scheduler_confirmation_eligibility(
        SchedulerConfirmationEligibilityRequest(
            db_path=request.db_path,
            task_key=request.task_key,
            proposal_item_id=request.proposal_item_id,
            proposal_hash=request.proposal_hash,
            proposal_id=request.proposal_id,
            item_hash=request.item_hash,
            recommended_command_kind=request.recommended_command_kind,
            expected_status=request.expected_status,
            proposal_artifact_path=request.proposal_artifact_path,
        )
    )

    if not eligibility.get("eligible"):
        return {
            "ok": False,
            "schema_version": CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
            "source": CONFIRMATION_FROM_PROPOSAL_SOURCE,
            "status": "not_eligible",
            "mode": _mode(request),
            "eligible": False,
            "task_key": request.task_key,
            "proposal_item_id": request.proposal_item_id,
            "reasons": list(eligibility.get("reasons") or []),
            "eligibility": eligibility,
            "confirmation": None,
            "safety": _safety(confirmation_created=False),
        }

    proposal_view = eligibility.get("proposal") or {}
    proposal_id = proposal_view.get("proposal_id")
    proposal_hash = proposal_view.get("proposal_hash")
    item_hash = proposal_view.get("item_hash")
    recommended_command_kind = proposal_view.get("recommended_command_kind")
    proposal_artifact_path = proposal_view.get("proposal_artifact_path")

    if not (
        isinstance(proposal_id, str)
        and isinstance(proposal_hash, str)
        and isinstance(item_hash, str)
    ):
        raise SchedulerConfirmationFromProposalError(
            "eligibility result missing proposal_id, proposal_hash, or item_hash"
        )

    confirmation_id = _make_confirmation_id(
        proposal_id=proposal_id,
        proposal_item_id=request.proposal_item_id,
        item_hash=item_hash,
    )
    created_at = utc_now_iso()

    artifact_path = (
        request.artifact_root
        / "scheduler_confirmations"
        / confirmation_id
        / "scheduler_confirmation.json"
    )

    eligibility_summary = {
        "eligible": True,
        "reasons": list(eligibility.get("reasons") or []),
        "warnings": list(eligibility.get("warnings") or []),
        "checks": dict(eligibility.get("checks") or {}),
        "current": dict(eligibility.get("current") or {}),
        "schema_version": eligibility.get("schema_version"),
    }

    confirmation_payload: dict[str, Any] = {
        "schema_version": CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
        "confirmation_id": confirmation_id,
        "created_at": created_at,
        "source": CONFIRMATION_FROM_PROPOSAL_SOURCE,
        "mode": "confirmed",
        "task_key": request.task_key,
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
        "proposal_item_id": request.proposal_item_id,
        "item_hash": item_hash,
        "recommended_command_kind": recommended_command_kind,
        "proposal_artifact_path": (
            str(proposal_artifact_path) if proposal_artifact_path else None
        ),
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path),
        "operator": request.operator,
        "operator_note": request.operator_note,
        "eligibility_summary": eligibility_summary,
        "not_execution_permission": True,
        "not_verifier_report": True,
        "not_handoff": True,
        "not_runtime": True,
        "requires_next_gate": True,
        "safety": _safety(confirmation_created=True),
    }

    if request.dry_run:
        return {
            "ok": True,
            "schema_version": CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
            "source": CONFIRMATION_FROM_PROPOSAL_SOURCE,
            "status": "dry_run",
            "mode": "dry_run",
            "eligible": True,
            "would_create_confirmation": True,
            "task_key": request.task_key,
            "proposal_item_id": request.proposal_item_id,
            "confirmation": confirmation_payload,
            "eligibility": eligibility,
            "safety": _safety(confirmation_created=False),
        }

    atomic_write_json(
        artifact_path,
        confirmation_payload,
        sort_keys=True,
        trailing_newline=False,
    )

    store = TaskMirrorStore(request.db_path)
    store.record_task_artifact(
        request.task_key,
        CONFIRMATION_ARTIFACT_TYPE,
        artifact_path,
    )
    event_payload: dict[str, Any] = {
        "kind": CONFIRMATION_EVENT_TYPE,
        "confirmation_id": confirmation_id,
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
        "proposal_item_id": request.proposal_item_id,
        "item_hash": item_hash,
        "task_key": request.task_key,
        "recommended_command_kind": recommended_command_kind,
        "proposal_artifact_path": (
            str(proposal_artifact_path) if proposal_artifact_path else None
        ),
        "artifact_path": str(artifact_path),
        "schema_version": CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
        "not_execution_permission": True,
        "not_verifier_report": True,
        "not_handoff": True,
        "not_runtime": True,
        "requires_next_gate": True,
    }
    store.record_task_event(
        request.task_key,
        CONFIRMATION_EVENT_TYPE,
        CONFIRMATION_FROM_PROPOSAL_SOURCE,
        message=(
            f"Scheduler confirmation {confirmation_id} recorded for "
            f"{recommended_command_kind} (audit only; not execution permission)"
        ),
        payload=event_payload,
    )

    return {
        "ok": True,
        "schema_version": CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
        "source": CONFIRMATION_FROM_PROPOSAL_SOURCE,
        "status": "created",
        "mode": "confirmed",
        "eligible": True,
        "task_key": request.task_key,
        "proposal_item_id": request.proposal_item_id,
        "confirmation": confirmation_payload,
        "eligibility": eligibility,
        "safety": _safety(confirmation_created=True),
    }


def _safety(*, confirmation_created: bool) -> dict[str, bool]:
    safety = dict(CONFIRMATION_SAFETY_FLAGS)
    safety["confirmation_created"] = confirmation_created
    return safety


def _mode(request: SchedulerConfirmationFromProposalRequest) -> str:
    return "dry_run" if request.dry_run else "confirmed"


def _make_confirmation_id(
    *,
    proposal_id: str,
    proposal_item_id: str,
    item_hash: str,
) -> str:
    timestamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "")
    digest = hashlib.sha256()
    digest.update(proposal_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(proposal_item_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(item_hash.encode("utf-8"))
    digest.update(b"|")
    digest.update(uuid4().hex.encode("utf-8"))
    return f"confirmation-{timestamp}-{digest.hexdigest()[:12]}"


__all__ = [
    "CONFIRMATION_ARTIFACT_TYPE",
    "CONFIRMATION_EVENT_TYPE",
    "CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION",
    "CONFIRMATION_FROM_PROPOSAL_SOURCE",
    "CONFIRMATION_SAFETY_FLAGS",
    "SchedulerConfirmationFromProposalError",
    "SchedulerConfirmationFromProposalRequest",
    "create_scheduler_confirmation_from_proposal",
]
