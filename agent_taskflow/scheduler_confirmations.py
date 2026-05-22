"""Scheduler confirmation artifact contract.

This module records operator pre-approval ("confirmation") of specific
hash-bound items from a previously recorded scheduler proposal artifact.
It is NOT an executor, NOT a runtime, and does NOT consume confirmation
artifacts. It does NOT mutate task lifecycle state, push branches, create
PRs, merge, approve/reject, run validators, run cleanup, contact GitHub,
or start any background worker.

A scheduler confirmation artifact is intentionally NOT action evidence.
Existing command-specific ``--confirm-*`` helpers remain the only
mutation gates. The confirmation artifact is audit/pre-approval only:
its existence does not grant execution permission. Future execution
design must still revalidate proposal + item hashes at consume time, and
the confirmation artifact must remain single-use in any future runtime
design.

The artifact and event types it may record
(``scheduler_confirmation`` / ``scheduler_confirmation_created``) are
intentionally disjoint from the workflow's action evidence types.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow.models import utc_now_iso
from agent_taskflow.scheduler_proposal_review import (
    SchedulerProposalReviewError,
    SchedulerProposalReviewRequest,
    review_scheduler_proposal,
)
from agent_taskflow.scheduler_proposals import (
    EXECUTABLE_COMMAND_KINDS,
    HASH_ALGORITHM,
)
from agent_taskflow.store import TaskMirrorStore


SCHEMA_VERSION = "scheduler_confirmation.v1"
CONFIRMATION_SOURCE = "scheduler_confirmations"
CONFIRMATION_ARTIFACT_TYPE = "scheduler_confirmation"
CONFIRMATION_EVENT_TYPE = "scheduler_confirmation_created"

# Recommendation kinds that are never confirmable as a workflow action.
# Confirmation does not execute, but a pre-approval for a non-actionable
# recommendation has no meaning and must be blocked.
NON_CONFIRMABLE_COMMAND_KINDS: frozenset[str] = frozenset(
    {"no_action", "unknown", "human_pr_review"}
)

# Recommendation kinds that may be pre-approved by a scheduler confirmation
# artifact. Confirmation does NOT execute; execution_allowed remains false.
CONFIRMABLE_COMMAND_KINDS: frozenset[str] = frozenset(
    EXECUTABLE_COMMAND_KINDS | {"inspect_blocker", "inspect_evidence"}
)

CONFIRMATION_SAFETY_FLAGS: dict[str, bool] = {
    "confirmation_only": True,
    "proposal_only": False,
    "action_evidence_created": False,
    "workflow_action_performed": False,
    "executor_started": False,
    "validators_started": False,
    "branch_pushed": False,
    "pr_created": False,
    "merged": False,
    "approved": False,
    "rejected": False,
    "cleanup_performed": False,
    "task_status_changed": False,
    "github_mutated": False,
    "background_worker_started": False,
    "execution_allowed": False,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_approve": False,
    "will_reject": False,
    "will_cleanup": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_github": False,
    "will_change_task_status": False,
    "will_start_background_worker": False,
}

_PROPOSAL_ID = re.compile(r"^proposal-[0-9A-Za-z._\-]+$")


class SchedulerConfirmationError(RuntimeError):
    """Raised when a scheduler confirmation cannot be safely produced."""


@dataclass(frozen=True)
class SchedulerConfirmationRequest:
    """Inputs to a scheduler confirmation.

    Exactly one of ``proposal_id``, ``proposal_artifact_path`` or
    ``latest`` must be supplied. ``selected_item_ids`` must list exact
    ``proposal_item_id`` values to pre-approve.

    Confirmation is audit/pre-approval only and never executes the
    underlying proposed action. Existing command-specific ``--confirm-*``
    helpers remain the only mutation gates.
    """

    db_path: Path
    artifact_root: Path
    proposal_id: str | None = None
    proposal_artifact_path: Path | None = None
    latest: bool = False
    selected_item_ids: tuple[str, ...] = field(default_factory=tuple)
    acknowledge_warnings: bool = False
    dry_run: bool = True
    confirm_create_confirmation: bool = False
    confirmed_by: str | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        artifact_root = Path(self.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        object.__setattr__(self, "artifact_root", artifact_root)

        if self.proposal_artifact_path is not None:
            pap = Path(self.proposal_artifact_path).expanduser()
            if not pap.is_absolute():
                raise ValueError(
                    "proposal_artifact_path must be an absolute path"
                )
            object.__setattr__(self, "proposal_artifact_path", pap)

        if self.proposal_id is not None:
            pid = self.proposal_id.strip()
            if not pid:
                raise ValueError("proposal_id must not be empty")
            if not _PROPOSAL_ID.match(pid):
                raise ValueError(f"invalid proposal_id format: {pid!r}")
            object.__setattr__(self, "proposal_id", pid)

        cleaned: list[str] = []
        for raw in self.selected_item_ids or ():
            if not isinstance(raw, str):
                raise ValueError("selected_item_ids must be strings")
            value = raw.strip()
            if not value:
                raise ValueError("selected_item_ids must not contain empty entries")
            if value not in cleaned:
                cleaned.append(value)
        object.__setattr__(self, "selected_item_ids", tuple(cleaned))

        if self.confirmed_by is not None:
            confirmed_by = self.confirmed_by.strip()
            object.__setattr__(
                self, "confirmed_by", confirmed_by if confirmed_by else None
            )


def create_scheduler_confirmation(
    request: SchedulerConfirmationRequest,
) -> dict[str, Any]:
    """Compute a scheduler confirmation payload and optionally persist it.

    Dry-run by default. Recording is gated by ``dry_run=False`` AND
    ``confirm_create_confirmation=True``; either alone is rejected.
    The confirmation artifact is never action evidence and never grants
    execution permission.
    """

    if not request.dry_run and not request.confirm_create_confirmation:
        raise SchedulerConfirmationError(
            "Non-dry-run scheduler confirmations require "
            "confirm_create_confirmation=True"
        )

    selectors = sum(
        1
        for selector in (
            request.proposal_id is not None,
            request.proposal_artifact_path is not None,
            request.latest,
        )
        if selector
    )
    if selectors == 0:
        raise SchedulerConfirmationError(
            "confirmation requires one of proposal_id, "
            "proposal_artifact_path, or latest=True"
        )
    if selectors > 1:
        raise SchedulerConfirmationError(
            "confirmation accepts only one of proposal_id, "
            "proposal_artifact_path, latest"
        )

    if not request.selected_item_ids:
        raise SchedulerConfirmationError(
            "confirmation requires at least one selected proposal_item_id"
        )

    try:
        review = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                proposal_id=request.proposal_id,
                artifact_path=request.proposal_artifact_path,
                latest=request.latest,
                include_items=True,
                verify_hashes=True,
            )
        )
    except SchedulerProposalReviewError as exc:
        raise SchedulerConfirmationError(
            f"could not review proposal: {exc}"
        ) from exc

    if review.get("review_status") != "valid":
        raise SchedulerConfirmationError(
            "proposal is not in 'valid' review state "
            f"(review_status={review.get('review_status')!r}, "
            f"error={review.get('error')!r}); confirmation refused"
        )

    proposal_id = review.get("proposal_id")
    proposal_hash = review.get("proposal_hash")
    proposal_artifact_path = review.get("artifact_path")
    if not (isinstance(proposal_id, str) and isinstance(proposal_hash, str)):
        raise SchedulerConfirmationError(
            "reviewed proposal is missing proposal_id or proposal_hash"
        )
    if not isinstance(proposal_artifact_path, str):
        raise SchedulerConfirmationError(
            "reviewed proposal is missing artifact_path"
        )

    hash_report = review.get("hash_report") or {}
    if not hash_report.get("proposal_hash_valid"):
        raise SchedulerConfirmationError(
            "proposal_hash is invalid; confirmation refused"
        )

    items_by_id: dict[str, dict[str, Any]] = {}
    for item in review.get("items") or []:
        if isinstance(item, dict):
            item_id = item.get("proposal_item_id")
            if isinstance(item_id, str):
                items_by_id[item_id] = item

    missing = [
        item_id
        for item_id in request.selected_item_ids
        if item_id not in items_by_id
    ]
    if missing:
        raise SchedulerConfirmationError(
            "selected proposal_item_id(s) not found in proposal: "
            + ", ".join(missing)
        )

    invalid_hashes = [
        item_id
        for item_id in request.selected_item_ids
        if items_by_id[item_id].get("item_hash_valid") is not True
    ]
    if invalid_hashes:
        raise SchedulerConfirmationError(
            "selected proposal_item_id(s) have invalid item_hash: "
            + ", ".join(invalid_hashes)
        )

    non_confirmable = []
    for item_id in request.selected_item_ids:
        kind = items_by_id[item_id].get("recommended_command_kind")
        if (
            kind in NON_CONFIRMABLE_COMMAND_KINDS
            or kind not in CONFIRMABLE_COMMAND_KINDS
        ):
            non_confirmable.append(f"{item_id}({kind})")
    if non_confirmable:
        raise SchedulerConfirmationError(
            "selected proposal_item_id(s) are not confirmable: "
            + ", ".join(non_confirmable)
        )

    warning_items = [
        item_id
        for item_id in request.selected_item_ids
        if items_by_id[item_id].get("consistency_warnings")
    ]
    if warning_items and not request.acknowledge_warnings:
        raise SchedulerConfirmationError(
            "selected proposal_item_id(s) have consistency_warnings and "
            "acknowledge_warnings=False: " + ", ".join(warning_items)
        )

    selected_items: list[dict[str, Any]] = []
    total_warnings = 0
    for item_id in request.selected_item_ids:
        item = items_by_id[item_id]
        warnings = list(item.get("consistency_warnings") or [])
        total_warnings += len(warnings)
        selected_items.append(
            {
                "proposal_item_id": item_id,
                "item_hash": item["item_hash"],
                "task_key": item.get("task_key"),
                "recommended_command_kind": item.get("recommended_command_kind"),
                "expected_status": item.get("expected_status"),
                "expected_phase_label": item.get("expected_phase_label"),
                "operator_acknowledged_warnings": (
                    bool(warnings) and request.acknowledge_warnings
                ),
                "consistency_warnings": warnings,
                "revalidation_required": True,
                "execution_allowed": False,
                "command_specific_confirmation_required": True,
            }
        )

    confirmation_id = _make_confirmation_id(proposal_id, selected_items)
    created_at = utc_now_iso()
    mode = "dry_run" if request.dry_run else "confirmed"

    artifact_path: Path | None = None
    if not request.dry_run:
        artifact_path = (
            request.artifact_root
            / "scheduler_confirmations"
            / confirmation_id
            / "scheduler_confirmation.json"
        )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "confirmation_id": confirmation_id,
        "created_at": created_at,
        "source": CONFIRMATION_SOURCE,
        "mode": mode,
        "hash_algorithm": HASH_ALGORITHM,
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path) if artifact_path else None,
        "confirmed_by": request.confirmed_by,
        "acknowledge_warnings": request.acknowledge_warnings,
        "proposal": {
            "proposal_id": proposal_id,
            "proposal_hash": proposal_hash,
            "proposal_artifact_path": proposal_artifact_path,
            "proposal_review_status": review.get("review_status"),
            "proposal_hash_valid": True,
        },
        "selected_items": selected_items,
        "summary": {
            "selected_item_count": len(selected_items),
            "warning_count": total_warnings,
            "execution_allowed": False,
            "workflow_action_performed": False,
            "requires_command_specific_confirmation": True,
            "confirmation_evidence_recorded": False,
        },
        "safety": dict(CONFIRMATION_SAFETY_FLAGS),
    }

    if request.dry_run:
        return payload

    assert artifact_path is not None
    payload["summary"]["confirmation_evidence_recorded"] = True
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    store = TaskMirrorStore(request.db_path)
    recorded_task_keys: set[str] = set()
    for item in selected_items:
        task_key = item.get("task_key")
        if not isinstance(task_key, str) or task_key in recorded_task_keys:
            continue
        recorded_task_keys.add(task_key)
        store.record_task_artifact(
            task_key,
            CONFIRMATION_ARTIFACT_TYPE,
            artifact_path,
        )
        store.record_task_event(
            task_key,
            CONFIRMATION_EVENT_TYPE,
            CONFIRMATION_SOURCE,
            message=(
                f"Scheduler confirmation {confirmation_id} pre-approves "
                f"{item['recommended_command_kind']} (audit only; no execution)"
            ),
            payload={
                "kind": CONFIRMATION_EVENT_TYPE,
                "confirmation_id": confirmation_id,
                "proposal_id": proposal_id,
                "proposal_hash": proposal_hash,
                "proposal_item_id": item["proposal_item_id"],
                "item_hash": item["item_hash"],
                "task_key": task_key,
                "recommended_command_kind": item["recommended_command_kind"],
                "execution_allowed": False,
                "workflow_action_performed": False,
                "action_evidence_created": False,
                "schema_version": SCHEMA_VERSION,
                "artifact_path": str(artifact_path),
            },
        )

    return payload


def _make_confirmation_id(
    proposal_id: str, selected_items: list[dict[str, Any]]
) -> str:
    timestamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "")
    digest = hashlib.sha256()
    digest.update(proposal_id.encode("utf-8"))
    for item in selected_items:
        digest.update(b"|")
        digest.update(str(item.get("proposal_item_id")).encode("utf-8"))
        digest.update(b":")
        digest.update(str(item.get("item_hash")).encode("utf-8"))
    digest.update(b"|")
    digest.update(uuid4().hex.encode("utf-8"))
    return f"confirmation-{timestamp}-{digest.hexdigest()[:12]}"


__all__ = [
    "CONFIRMABLE_COMMAND_KINDS",
    "CONFIRMATION_ARTIFACT_TYPE",
    "CONFIRMATION_EVENT_TYPE",
    "CONFIRMATION_SAFETY_FLAGS",
    "CONFIRMATION_SOURCE",
    "NON_CONFIRMABLE_COMMAND_KINDS",
    "SCHEMA_VERSION",
    "SchedulerConfirmationError",
    "SchedulerConfirmationRequest",
    "create_scheduler_confirmation",
]
