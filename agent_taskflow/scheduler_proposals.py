"""Read-only scheduler proposal contract.

This module reads task recommendations and produces a reviewable scheduler
proposal payload. It is NOT a scheduler. It does NOT run a background loop.
It does NOT poll, run as a webhook, or run as cron. It does NOT execute
workflow actions. It does NOT mutate task lifecycle state.

A scheduler proposal is NEVER action evidence. The artifact and event types
it may record (`scheduler_proposal` / `scheduler_proposal_created`) are
intentionally disjoint from the workflow's action evidence types and must
never be interpreted by downstream readers as proof that a proposed action
ran.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow.models import utc_now_iso, validate_task_status
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_recommendations import (
    COMPLETED_STATUSES,
    RECOMMENDED_COMMAND_KINDS,
    TaskRecommendationsError,
    TaskRecommendationsRequest,
    list_task_recommendations,
)
from agent_taskflow.tasks import normalize_task_key


SCHEMA_VERSION = "scheduler_proposal.v1"
PROPOSAL_SOURCE = "scheduler_proposals"
PROPOSAL_ARTIFACT_TYPE = "scheduler_proposal"
PROPOSAL_EVENT_TYPE = "scheduler_proposal_created"
DEFAULT_POLICY_NAME = "default_read_only_proposal_policy"
DEFAULT_MAX_ITEMS = 20

HASH_ALGORITHM = "sha256"
PROPOSAL_HASH_PAYLOAD_VERSION = "scheduler_proposal_hash.v1"
ITEM_HASH_PAYLOAD_VERSION = "scheduler_proposal_item_hash.v1"

DEFAULT_ACTIONABLE_COMMAND_KINDS: tuple[str, ...] = (
    "create_task_execution_package",
    "queued_task_handoff",
    "pr_handoff_package",
    "branch_push_review",
    "draft_pr_review",
    "human_pr_review",
    "post_merge_cleanup_review",
    "cleanup_continue",
    "inspect_blocker",
    "inspect_evidence",
)

EXECUTABLE_COMMAND_KINDS: frozenset[str] = frozenset(
    {
        "create_task_execution_package",
        "queued_task_handoff",
        "pr_handoff_package",
        "branch_push_review",
        "draft_pr_review",
        "post_merge_cleanup_review",
        "cleanup_continue",
    }
)

COMMAND_KIND_PRIORITY: dict[str, int] = {
    "inspect_blocker": 1,
    "inspect_evidence": 2,
    "cleanup_continue": 3,
    "post_merge_cleanup_review": 4,
    "human_pr_review": 5,
    "draft_pr_review": 6,
    "branch_push_review": 7,
    "pr_handoff_package": 8,
    "queued_task_handoff": 9,
    "create_task_execution_package": 10,
    "unknown": 11,
    "no_action": 12,
}

_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2, "info": 3}

ITEM_SAFETY_FLAGS: dict[str, bool] = {
    "proposal_only": True,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_approve": False,
    "will_cleanup": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_github": False,
    "will_mutate_db_as_action": False,
    "will_start_background_worker": False,
}

PROPOSAL_SAFETY_FLAGS: dict[str, bool] = {
    "read_only_scan": True,
    "proposal_only": True,
    "action_evidence_created": False,
    "workflow_action_performed": False,
    "executor_started": False,
    "validators_started": False,
    "branch_pushed": False,
    "pr_created": False,
    "merged": False,
    "approved": False,
    "cleanup_performed": False,
    "background_worker_started": False,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_approve": False,
    "will_cleanup": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_github": False,
    "will_start_background_worker": False,
}


class SchedulerProposalError(RuntimeError):
    """Raised when a scheduler proposal cannot be safely produced or recorded."""


@dataclass(frozen=True)
class SchedulerProposalRequest:
    """Inputs to a scheduler proposal computation."""

    db_path: Path
    artifact_root: Path
    status: str | None = None
    project: str | None = None
    task_key: str | None = None
    include_completed: bool = False
    include_no_action: bool = False
    include_unknown: bool = False
    include_command_kinds: tuple[str, ...] | None = None
    exclude_command_kinds: tuple[str, ...] = ()
    max_items: int = DEFAULT_MAX_ITEMS
    dry_run: bool = True
    confirm_create_proposal: bool = False

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        artifact_root = Path(self.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        object.__setattr__(self, "artifact_root", artifact_root)

        if self.status is not None:
            object.__setattr__(self, "status", validate_task_status(self.status))

        if self.project is not None:
            project = self.project.strip()
            if not project:
                raise ValueError("project must not be empty")
            object.__setattr__(self, "project", project)

        if self.task_key is not None:
            object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        if self.max_items < 0:
            raise ValueError("max_items must be zero or positive")

        exclude = tuple(self.exclude_command_kinds or ())
        for kind in exclude:
            if kind not in RECOMMENDED_COMMAND_KINDS:
                raise ValueError(f"unknown command kind in exclude: {kind!r}")
        object.__setattr__(self, "exclude_command_kinds", exclude)

        if self.include_command_kinds is not None:
            include = tuple(self.include_command_kinds)
            for kind in include:
                if kind not in RECOMMENDED_COMMAND_KINDS:
                    raise ValueError(f"unknown command kind in include: {kind!r}")
            object.__setattr__(self, "include_command_kinds", include)


def create_scheduler_proposal(request: SchedulerProposalRequest) -> dict[str, Any]:
    """Compute a scheduler proposal payload and optionally persist it.

    Read-only by default. Recording is gated by `dry_run=False` AND
    `confirm_create_proposal=True`; either alone is rejected.
    """

    if not request.dry_run and not request.confirm_create_proposal:
        raise SchedulerProposalError(
            "Non-dry-run scheduler proposals require confirm_create_proposal=True"
        )

    if not request.db_path.exists():
        raise SchedulerProposalError(
            f"SQLite state DB not found: {request.db_path}"
        )

    try:
        rec_payload = list_task_recommendations(
            TaskRecommendationsRequest(
                db_path=request.db_path,
                status=request.status,
                project=request.project,
                task_key=request.task_key,
                completed_limit=20 if request.include_completed else 0,
            )
        )
    except TaskRecommendationsError as exc:
        raise SchedulerProposalError(f"could not read recommendations: {exc}") from exc

    raw_items = rec_payload.get("items", [])
    policy = _resolved_policy(request)

    candidates = [
        candidate
        for candidate in (_build_candidate(item, request) for item in raw_items)
        if candidate is not None
    ]
    candidate_count = len(candidates)
    candidates.sort(key=_sort_key)
    selected = candidates[: request.max_items]

    for item in selected:
        item["item_hash"] = _compute_item_hash(item)

    proposal_id = _make_proposal_id()
    created_at = utc_now_iso()
    mode = "dry_run" if request.dry_run else "confirmed"

    artifact_path: Path | None = None
    if not request.dry_run:
        artifact_path = (
            request.artifact_root
            / "scheduler_proposals"
            / proposal_id
            / "scheduler_proposal.json"
        )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "created_at": created_at,
        "source": PROPOSAL_SOURCE,
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path) if artifact_path else None,
        "mode": mode,
        "hash_algorithm": HASH_ALGORITHM,
        "proposal_hash_payload_version": PROPOSAL_HASH_PAYLOAD_VERSION,
        "item_hash_payload_version": ITEM_HASH_PAYLOAD_VERSION,
        "filters": {
            "status": request.status,
            "project": request.project,
            "task_key": request.task_key,
            "include_completed": request.include_completed,
        },
        "policy": policy,
        "items": selected,
        "summary": {
            "item_count": len(selected),
            "candidate_count": candidate_count,
            "warning_count": sum(
                len(item["consistency_warnings"]) for item in selected
            ),
            "executable_count": sum(1 for item in selected if item["executable"]),
            "mutation_performed": False,
            "requires_human_review": True,
            "requires_explicit_confirmation_before_execution": True,
            "proposal_evidence_recorded": False,
        },
        "safety": dict(PROPOSAL_SAFETY_FLAGS),
    }
    payload["proposal_hash"] = _compute_proposal_hash(payload)

    if request.dry_run:
        return payload

    assert artifact_path is not None
    payload["summary"]["proposal_evidence_recorded"] = True
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    if selected:
        store = TaskMirrorStore(request.db_path)
        for item in selected:
            store.record_task_artifact(
                item["task_key"],
                PROPOSAL_ARTIFACT_TYPE,
                artifact_path,
            )
            store.record_task_event(
                item["task_key"],
                PROPOSAL_EVENT_TYPE,
                PROPOSAL_SOURCE,
                message=(
                    f"Scheduler proposal {proposal_id} suggests "
                    f"{item['recommended_command_kind']}"
                ),
                payload={
                    "kind": PROPOSAL_EVENT_TYPE,
                    "proposal_id": proposal_id,
                    "proposal_hash": payload["proposal_hash"],
                    "proposal_item_id": item["proposal_item_id"],
                    "item_hash": item["item_hash"],
                    "task_key": item["task_key"],
                    "recommended_command_kind": item["recommended_command_kind"],
                    "executable": item["executable"],
                    "consistency_warning_count": len(item["consistency_warnings"]),
                    "selected_item_count": len(selected),
                    "artifact_path": str(artifact_path),
                    "schema_version": SCHEMA_VERSION,
                },
            )

    return payload


def propose_tasks(
    db_path: str | Path,
    artifact_root: str | Path,
    *,
    status: str | None = None,
    project: str | None = None,
    task_key: str | None = None,
    include_completed: bool = False,
    include_no_action: bool = False,
    include_unknown: bool = False,
    max_items: int = DEFAULT_MAX_ITEMS,
    dry_run: bool = True,
    confirm_create_proposal: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper for callers that do not need a request object."""

    request = SchedulerProposalRequest(
        db_path=Path(db_path),
        artifact_root=Path(artifact_root),
        status=status,
        project=project,
        task_key=task_key,
        include_completed=include_completed,
        include_no_action=include_no_action,
        include_unknown=include_unknown,
        max_items=max_items,
        dry_run=dry_run,
        confirm_create_proposal=confirm_create_proposal,
    )
    return create_scheduler_proposal(request)


def _resolved_policy(request: SchedulerProposalRequest) -> dict[str, Any]:
    include_command_kinds = (
        list(request.include_command_kinds)
        if request.include_command_kinds is not None
        else list(DEFAULT_ACTIONABLE_COMMAND_KINDS)
    )
    return {
        "name": DEFAULT_POLICY_NAME,
        "mode": "proposal_only",
        "max_items": request.max_items,
        "include_command_kinds": include_command_kinds,
        "exclude_command_kinds": list(request.exclude_command_kinds),
        "include_completed": request.include_completed,
        "include_no_action": request.include_no_action,
        "include_unknown": request.include_unknown,
    }


def _build_candidate(
    rec_item: dict[str, Any],
    request: SchedulerProposalRequest,
) -> dict[str, Any] | None:
    status = rec_item["status"]
    kind = rec_item["recommended_command_kind"]
    severity = rec_item.get("severity") or "info"

    if status in COMPLETED_STATUSES and not request.include_completed:
        return None
    if kind in request.exclude_command_kinds:
        return None
    if kind == "no_action" and not (
        request.include_no_action or request.include_completed
    ):
        return None
    if kind == "unknown":
        if not request.include_unknown and severity not in {"high", "medium"}:
            return None

    if request.include_command_kinds is not None:
        if kind not in request.include_command_kinds:
            return None
    else:
        if (
            kind not in DEFAULT_ACTIONABLE_COMMAND_KINDS
            and kind != "no_action"
            and kind != "unknown"
        ):
            return None

    consistency_warnings = list(rec_item.get("consistency_warnings") or [])
    executable = kind in EXECUTABLE_COMMAND_KINDS and not consistency_warnings
    priority_rank = COMMAND_KIND_PRIORITY.get(kind, 99)
    task_key = rec_item["task_key"]
    current_phase_label = rec_item.get("current_phase_label")
    missing_evidence = list(rec_item.get("missing_evidence") or [])

    return {
        "task_key": task_key,
        "project": rec_item.get("project"),
        "title": rec_item.get("title"),
        "status": status,
        "expected_status": status,
        "proposal_item_id": f"{task_key}:{kind}",
        "current_phase_label": current_phase_label,
        "expected_phase_label": current_phase_label,
        "recommended_command_kind": kind,
        "proposed_action": rec_item.get("recommended_next_action"),
        "reason": rec_item.get("reason"),
        "severity": severity,
        "confidence": rec_item.get("confidence"),
        "requires_human_confirmation": True,
        "executable": executable,
        "consistency_warnings": consistency_warnings,
        "missing_evidence": missing_evidence,
        "expected_evidence_summary": _build_expected_evidence_summary(rec_item),
        "expected_refs": _build_expected_refs(rec_item),
        "priority_rank": priority_rank,
        "safety_flags": dict(ITEM_SAFETY_FLAGS),
    }


def _build_expected_refs(rec_item: dict[str, Any]) -> dict[str, Any]:
    """Stable, normalized references the future confirmation must revalidate.

    Only fields that can be derived safely from the recommendation item are
    included. Missing fields are present as ``None`` so the shape is stable.
    """

    worktree = rec_item.get("worktree_status") or {}
    branch = rec_item.get("branch_status") or {}
    pr = rec_item.get("pr_status") or {}
    return {
        "worktree_path": worktree.get("worktree_path"),
        "worktree_exists": worktree.get("path_exists"),
        "branch": branch.get("branch") or worktree.get("branch"),
        "base_branch": branch.get("base_branch") or worktree.get("base_branch"),
        "base_sha": worktree.get("base_sha"),
        "head_sha": branch.get("head_sha"),
        "pr_number": pr.get("pr_number"),
        "pr_url": pr.get("pr_url"),
        "pr_state": pr.get("state"),
        "pr_is_draft": pr.get("draft_pr"),
    }


def _build_expected_evidence_summary(rec_item: dict[str, Any]) -> dict[str, Any]:
    """Stable, normalized subset of recommendation evidence.

    Includes only semantic fields. ``related_artifacts`` is reduced to
    ``artifact_type``/``path`` and sorted deterministically so artifact
    discovery order does not affect the hash.
    """

    related = [
        {
            "artifact_type": artifact.get("artifact_type"),
            "path": artifact.get("path"),
        }
        for artifact in (rec_item.get("related_artifacts") or [])
    ]
    related.sort(key=lambda r: (r.get("artifact_type") or "", r.get("path") or ""))
    return {
        "evidence_summary": dict(rec_item.get("evidence_summary") or {}),
        "missing_evidence": list(rec_item.get("missing_evidence") or []),
        "consistency_warnings": list(rec_item.get("consistency_warnings") or []),
        "worktree_status": dict(rec_item.get("worktree_status") or {}),
        "branch_status": dict(rec_item.get("branch_status") or {}),
        "pr_status": dict(rec_item.get("pr_status") or {}),
        "cleanup_status": dict(rec_item.get("cleanup_status") or {}),
        "related_artifacts": related,
    }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _item_hash_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Semantic fields bound by ``item_hash``.

    Excludes display-only fields (``project``, ``title``), the hash itself,
    and proposal-level identifiers (``proposal_id`` / ``proposal_hash``) so
    the same item produced under a different proposal instance hashes the
    same.
    """

    return {
        "proposal_item_id": item["proposal_item_id"],
        "task_key": item["task_key"],
        "status": item["status"],
        "expected_status": item["expected_status"],
        "recommended_command_kind": item["recommended_command_kind"],
        "current_phase_label": item["current_phase_label"],
        "expected_phase_label": item["expected_phase_label"],
        "proposed_action": item["proposed_action"],
        "reason": item["reason"],
        "severity": item["severity"],
        "confidence": item["confidence"],
        "requires_human_confirmation": item["requires_human_confirmation"],
        "executable": item["executable"],
        "consistency_warnings": list(item["consistency_warnings"]),
        "missing_evidence": list(item["missing_evidence"]),
        "expected_evidence_summary": item["expected_evidence_summary"],
        "expected_refs": item["expected_refs"],
        "priority_rank": item["priority_rank"],
        "safety_flags": dict(item["safety_flags"]),
        "item_hash_payload_version": ITEM_HASH_PAYLOAD_VERSION,
    }


def _compute_item_hash(item: dict[str, Any]) -> str:
    return _sha256_hex(_item_hash_payload(item))


def _proposal_hash_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Semantic fields bound by ``proposal_hash``.

    ``proposal_id`` is intentionally excluded so the hash represents the
    proposal's semantic contents and not the instance identifier. Future
    confirmation artifacts bind to both ``proposal_id`` (the instance) AND
    ``proposal_hash`` (the contents). ``created_at``, ``artifact_path``, and
    ``mode`` are also excluded so the hash is identical across dry-run and
    confirmed renderings of the same input.
    """

    summary = payload.get("summary") or {}
    semantic_summary = {
        "item_count": summary.get("item_count"),
        "candidate_count": summary.get("candidate_count"),
        "warning_count": summary.get("warning_count"),
        "executable_count": summary.get("executable_count"),
    }
    items_binding = [
        {
            "proposal_item_id": item["proposal_item_id"],
            "task_key": item["task_key"],
            "recommended_command_kind": item["recommended_command_kind"],
            "item_hash": item["item_hash"],
        }
        for item in payload.get("items", [])
    ]
    return {
        "schema_version": payload["schema_version"],
        "source": payload["source"],
        "filters": payload["filters"],
        "policy": payload["policy"],
        "summary": semantic_summary,
        "items": items_binding,
        "safety": payload["safety"],
        "hash_algorithm": HASH_ALGORITHM,
        "proposal_hash_payload_version": PROPOSAL_HASH_PAYLOAD_VERSION,
    }


def _compute_proposal_hash(payload: dict[str, Any]) -> str:
    return _sha256_hex(_proposal_hash_payload(payload))


def compute_item_hash(item: dict[str, Any]) -> str:
    """Recompute the sha256 ``item_hash`` for a scheduler proposal item.

    Stable public entry point for callers (e.g. the review surface) that
    need to verify hash binding without depending on private helpers. Does
    not mutate ``item``.
    """

    return _compute_item_hash(item)


def compute_proposal_hash(payload: dict[str, Any]) -> str:
    """Recompute the sha256 ``proposal_hash`` for a scheduler proposal payload.

    Stable public entry point. Does not mutate ``payload``.
    """

    return _compute_proposal_hash(payload)


def verify_proposal_hashes(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify ``proposal_hash`` and per-item ``item_hash`` against a recompute.

    Returns a hash verification report. Does not mutate ``payload``. The
    returned dict has keys:

    - ``hash_algorithm``
    - ``proposal_hash_payload_version``
    - ``item_hash_payload_version``
    - ``proposal_hash_valid`` (bool)
    - ``expected_proposal_hash`` (recomputed) / ``actual_proposal_hash``
    - ``items``: per-item reports with ``proposal_item_id``,
      ``item_hash_valid``, ``expected_item_hash``, ``actual_item_hash``

    The function intentionally cannot distinguish a tampering attacker who
    re-hashes the entire payload self-consistently; that requires a
    signature, which is out of scope.
    """

    items_in = payload.get("items") or []
    item_reports: list[dict[str, Any]] = []
    for item in items_in:
        if not isinstance(item, dict):
            item_reports.append(
                {
                    "proposal_item_id": None,
                    "item_hash_valid": False,
                    "expected_item_hash": None,
                    "actual_item_hash": None,
                }
            )
            continue
        actual = item.get("item_hash")
        try:
            expected = _compute_item_hash(item)
        except KeyError:
            item_reports.append(
                {
                    "proposal_item_id": item.get("proposal_item_id"),
                    "item_hash_valid": False,
                    "expected_item_hash": None,
                    "actual_item_hash": actual if isinstance(actual, str) else None,
                }
            )
            continue
        item_reports.append(
            {
                "proposal_item_id": item.get("proposal_item_id"),
                "item_hash_valid": actual == expected,
                "expected_item_hash": expected,
                "actual_item_hash": actual if isinstance(actual, str) else None,
            }
        )

    actual_proposal = payload.get("proposal_hash")
    try:
        expected_proposal = _compute_proposal_hash(payload)
    except KeyError:
        expected_proposal = None
    return {
        "hash_algorithm": HASH_ALGORITHM,
        "proposal_hash_payload_version": PROPOSAL_HASH_PAYLOAD_VERSION,
        "item_hash_payload_version": ITEM_HASH_PAYLOAD_VERSION,
        "proposal_hash_valid": (
            isinstance(actual_proposal, str)
            and isinstance(expected_proposal, str)
            and actual_proposal == expected_proposal
        ),
        "expected_proposal_hash": expected_proposal,
        "actual_proposal_hash": actual_proposal if isinstance(actual_proposal, str) else None,
        "items": item_reports,
    }


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    severity_rank = _SEVERITY_RANK.get(item.get("severity") or "info", 4)
    return (item["priority_rank"], severity_rank, item["task_key"])


def _make_proposal_id() -> str:
    timestamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "")
    return f"proposal-{timestamp}-{uuid4().hex[:8]}"


__all__ = [
    "COMMAND_KIND_PRIORITY",
    "DEFAULT_ACTIONABLE_COMMAND_KINDS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_POLICY_NAME",
    "EXECUTABLE_COMMAND_KINDS",
    "HASH_ALGORITHM",
    "ITEM_HASH_PAYLOAD_VERSION",
    "ITEM_SAFETY_FLAGS",
    "PROPOSAL_ARTIFACT_TYPE",
    "PROPOSAL_EVENT_TYPE",
    "PROPOSAL_HASH_PAYLOAD_VERSION",
    "PROPOSAL_SAFETY_FLAGS",
    "PROPOSAL_SOURCE",
    "SCHEMA_VERSION",
    "SchedulerProposalError",
    "SchedulerProposalRequest",
    "compute_item_hash",
    "compute_proposal_hash",
    "create_scheduler_proposal",
    "propose_tasks",
    "verify_proposal_hashes",
]
