"""Dry-run-only scheduler confirmation consumption verifier.

This module answers a single read-only question about one
``scheduler_confirmation`` artifact item:

    "Would this exact scheduler_confirmation item be valid to attempt
     consumption now?"

It is NOT a consumer, NOT an executor, NOT a runtime, NOT a scheduler.

It does NOT:

- execute any proposed action,
- consume the confirmation as a state mutation,
- write consumption evidence,
- mutate task lifecycle state, push branches, create PRs, merge,
  approve/reject, run validators, run cleanup, contact GitHub, or start
  any background worker,
- emit a ``scheduler_confirmation_consumed`` event or a
  ``scheduler_confirmation_consumption`` artifact,
- bypass the existing command-specific ``--confirm-*`` helpers.

The verifier output is itself an in-memory dry-run report. It is never
action evidence and must never be interpreted as such by downstream
readers. Its sole purpose is to surface the pass/fail of the binding +
revalidation + expiration checks described in
``docs/single-item-confirmation-consumption-boundary.md``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_taskflow.scheduler_confirmations import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMABLE_COMMAND_KINDS,
    NON_CONFIRMABLE_COMMAND_KINDS,
    SCHEMA_VERSION as CONFIRMATION_SCHEMA_VERSION,
)
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    build_proposal_candidate,
    compute_item_hash,
)
from agent_taskflow.task_recommendations import (
    TaskRecommendationsError,
    TaskRecommendationsRequest,
    list_task_recommendations,
)


VERIFICATION_SCHEMA_VERSION = "scheduler_confirmation_verification.v1"
VERIFIER_SOURCE = "scheduler_confirmation_verifier"

# Verification status namespace.
STATUS_VALID = "valid"
STATUS_BLOCKED = "blocked"
STATUS_NOT_FOUND = "not_found"
STATUS_INVALID = "invalid"

VERIFICATION_STATUSES: tuple[str, ...] = (
    STATUS_VALID,
    STATUS_BLOCKED,
    STATUS_NOT_FOUND,
    STATUS_INVALID,
)

# Default expirations by recommended_command_kind, in minutes. These
# mirror the table in docs/single-item-confirmation-consumption-boundary.md
# §6 and docs/proposal-review-batch-confirmation-boundary.md §5.
DEFAULT_EXPIRATION_MINUTES: dict[str, int] = {
    "branch_push_review": 15,
    "draft_pr_review": 15,
    "queued_task_handoff": 15,
    "cleanup_continue": 15,
    "post_merge_cleanup_review": 15,
    "create_task_execution_package": 30,
    "pr_handoff_package": 30,
    "inspect_blocker": 24 * 60,
    "inspect_evidence": 24 * 60,
}

# Verifier output safety flags. Every flag below is always emitted as
# false so a downstream reader cannot misinterpret a verifier report as
# permission to act.
VERIFIER_SAFETY_FLAGS: dict[str, bool] = {
    "dry_run_only": True,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_approve": False,
    "will_reject": False,
    "will_cleanup": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_db": False,
    "will_mutate_github": False,
    "will_change_task_status": False,
    "will_start_background_worker": False,
}

_CONFIRMATION_ID = re.compile(r"^confirmation-[0-9A-Za-z._\-]+$")


class SchedulerConfirmationVerifierError(RuntimeError):
    """Raised when a verification request cannot be honored."""


@dataclass(frozen=True)
class SchedulerConfirmationVerificationRequest:
    """Inputs to a scheduler confirmation verification.

    Exactly one of ``confirmation_id``, ``confirmation_artifact_path``,
    or ``latest=True`` must be supplied. ``proposal_item_id`` selects the
    single item within the confirmation to verify.

    The verifier is read-only. It does not consume the confirmation.
    """

    db_path: Path
    proposal_item_id: str
    artifact_root: Path | None = None
    confirmation_id: str | None = None
    confirmation_artifact_path: Path | None = None
    latest: bool = False
    expected_command_kind: str | None = None
    task_key: str | None = None
    max_age_minutes: int | None = None
    now: datetime | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        if self.artifact_root is not None:
            ar = Path(self.artifact_root).expanduser()
            if not ar.is_absolute():
                raise ValueError("artifact_root must be an absolute path")
            object.__setattr__(self, "artifact_root", ar)

        if self.confirmation_artifact_path is not None:
            cap = Path(self.confirmation_artifact_path).expanduser()
            if not cap.is_absolute():
                raise ValueError(
                    "confirmation_artifact_path must be an absolute path"
                )
            object.__setattr__(self, "confirmation_artifact_path", cap)

        if self.confirmation_id is not None:
            cid = self.confirmation_id.strip()
            if not cid:
                raise ValueError("confirmation_id must not be empty")
            if not _CONFIRMATION_ID.match(cid):
                raise ValueError(f"invalid confirmation_id format: {cid!r}")
            object.__setattr__(self, "confirmation_id", cid)

        item_id = self.proposal_item_id.strip() if self.proposal_item_id else ""
        if not item_id:
            raise ValueError("proposal_item_id must be a non-empty string")
        object.__setattr__(self, "proposal_item_id", item_id)

        if self.expected_command_kind is not None:
            kind = self.expected_command_kind.strip()
            object.__setattr__(
                self, "expected_command_kind", kind if kind else None
            )

        if self.task_key is not None:
            tk = self.task_key.strip()
            object.__setattr__(self, "task_key", tk if tk else None)

        if self.max_age_minutes is not None and self.max_age_minutes < 0:
            raise ValueError("max_age_minutes must be zero or positive")


def verify_scheduler_confirmation_item(
    request: SchedulerConfirmationVerificationRequest,
) -> dict[str, Any]:
    """Run binding + revalidation + expiration checks for one item.

    Always read-only. Returns a structured report describing the outcome
    of every check; ``allowed_to_attempt`` is true only when every check
    passes. Never writes a consumption artifact, never records an event,
    never mutates DB or filesystem state.
    """

    base = _base_payload(request)

    selectors = sum(
        1
        for selector in (
            request.confirmation_id is not None,
            request.confirmation_artifact_path is not None,
            request.latest,
        )
        if selector
    )
    if selectors == 0:
        raise SchedulerConfirmationVerifierError(
            "verification requires one of confirmation_id, "
            "confirmation_artifact_path, or latest=True"
        )
    if selectors > 1:
        raise SchedulerConfirmationVerifierError(
            "verification accepts only one of confirmation_id, "
            "confirmation_artifact_path, latest"
        )

    if not request.db_path.exists():
        raise SchedulerConfirmationVerifierError(
            f"SQLite state DB not found: {request.db_path}"
        )

    artifact_path = _locate_confirmation_artifact(request)
    if artifact_path is None:
        return _finalize(
            base,
            status=STATUS_NOT_FOUND,
            checks=[
                {
                    "name": "confirmation_artifact_found",
                    "passed": False,
                    "detail": "no scheduler_confirmation artifact matched the requested selector",
                }
            ],
        )

    base["confirmation_artifact_path"] = str(artifact_path)
    if not artifact_path.exists():
        return _finalize(
            base,
            status=STATUS_NOT_FOUND,
            checks=[
                {
                    "name": "confirmation_artifact_found",
                    "passed": False,
                    "detail": f"file not found on disk: {artifact_path}",
                }
            ],
        )

    try:
        raw = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _finalize(
            base,
            status=STATUS_INVALID,
            checks=[
                {
                    "name": "confirmation_artifact_readable",
                    "passed": False,
                    "detail": f"could not read artifact: {exc}",
                }
            ],
        )

    try:
        on_disk = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _finalize(
            base,
            status=STATUS_INVALID,
            checks=[
                {
                    "name": "confirmation_artifact_readable",
                    "passed": False,
                    "detail": f"could not parse artifact JSON: {exc}",
                }
            ],
        )

    if not isinstance(on_disk, dict):
        return _finalize(
            base,
            status=STATUS_INVALID,
            checks=[
                {
                    "name": "confirmation_artifact_readable",
                    "passed": False,
                    "detail": "artifact JSON root is not an object",
                }
            ],
        )

    checks: list[dict[str, Any]] = [
        {
            "name": "confirmation_artifact_found",
            "passed": True,
            "detail": str(artifact_path),
        },
        {
            "name": "confirmation_artifact_readable",
            "passed": True,
            "detail": None,
        },
    ]

    _absorb_confirmation_metadata(base, on_disk)

    schema_error = _schema_error(on_disk)
    checks.append(
        {
            "name": "confirmation_schema_supported",
            "passed": schema_error is None,
            "detail": schema_error,
        }
    )
    if schema_error is not None:
        return _finalize(base, status=STATUS_INVALID, checks=checks)

    safety_error = _confirmation_safety_error(on_disk)
    checks.append(
        {
            "name": "confirmation_safety_payload_safe",
            "passed": safety_error is None,
            "detail": safety_error,
        }
    )
    if safety_error is not None:
        return _finalize(base, status=STATUS_INVALID, checks=checks)

    selected_item = _select_item(on_disk, request.proposal_item_id)
    checks.append(
        {
            "name": "selected_proposal_item_present",
            "passed": selected_item is not None,
            "detail": (
                None
                if selected_item is not None
                else f"proposal_item_id {request.proposal_item_id!r} not present in confirmation.selected_items"
            ),
        }
    )
    if selected_item is None:
        return _finalize(base, status=STATUS_BLOCKED, checks=checks)

    _absorb_item_metadata(base, selected_item)

    item_field_error = _item_field_error(selected_item)
    checks.append(
        {
            "name": "selected_item_fields_present",
            "passed": item_field_error is None,
            "detail": item_field_error,
        }
    )
    if item_field_error is not None:
        return _finalize(base, status=STATUS_INVALID, checks=checks)

    kind = selected_item["recommended_command_kind"]
    kind_passed = (
        kind in CONFIRMABLE_COMMAND_KINDS
        and kind not in NON_CONFIRMABLE_COMMAND_KINDS
        and kind in DEFAULT_EXPIRATION_MINUTES
    )
    checks.append(
        {
            "name": "recommended_command_kind_is_consumable",
            "passed": kind_passed,
            "detail": (
                None
                if kind_passed
                else f"{kind!r} is not consumable by this verifier"
            ),
        }
    )
    if not kind_passed:
        return _finalize(base, status=STATUS_BLOCKED, checks=checks)

    if request.expected_command_kind is not None:
        ok = kind == request.expected_command_kind
        checks.append(
            {
                "name": "expected_command_kind_matches",
                "passed": ok,
                "detail": (
                    None
                    if ok
                    else f"expected {request.expected_command_kind!r}, item is {kind!r}"
                ),
            }
        )
        if not ok:
            return _finalize(base, status=STATUS_BLOCKED, checks=checks)

    if request.task_key is not None:
        ok = selected_item.get("task_key") == request.task_key
        checks.append(
            {
                "name": "expected_task_key_matches",
                "passed": ok,
                "detail": (
                    None
                    if ok
                    else (
                        f"expected task_key {request.task_key!r}, item is "
                        f"{selected_item.get('task_key')!r}"
                    )
                ),
            }
        )
        if not ok:
            return _finalize(base, status=STATUS_BLOCKED, checks=checks)

    warnings = list(selected_item.get("consistency_warnings") or [])
    if warnings:
        acknowledged = bool(selected_item.get("operator_acknowledged_warnings"))
        checks.append(
            {
                "name": "confirmation_warnings_acknowledged",
                "passed": acknowledged,
                "detail": (
                    None
                    if acknowledged
                    else "item carries consistency_warnings but operator_acknowledged_warnings=false"
                ),
            }
        )
        if not acknowledged:
            return _finalize(base, status=STATUS_BLOCKED, checks=checks)
    else:
        checks.append(
            {
                "name": "confirmation_warnings_acknowledged",
                "passed": True,
                "detail": "no warnings on selected item",
            }
        )

    expiration = _check_expiration(
        on_disk_created_at=on_disk.get("created_at"),
        kind=kind,
        max_age_minutes_override=request.max_age_minutes,
        now=request.now,
    )
    base["expiration"] = expiration
    checks.append(
        {
            "name": "confirmation_not_expired",
            "passed": not expiration["expired"],
            "detail": expiration.get("detail"),
        }
    )
    if expiration["expired"]:
        return _finalize(base, status=STATUS_BLOCKED, checks=checks)

    revalidation = _revalidate_current_state(
        db_path=request.db_path,
        selected_item=selected_item,
    )
    base["revalidation"] = revalidation["report"]
    for revalidation_check in revalidation["checks"]:
        checks.append(revalidation_check)
        if not revalidation_check["passed"]:
            return _finalize(base, status=STATUS_BLOCKED, checks=checks)

    return _finalize(base, status=STATUS_VALID, checks=checks)


# --- Internal helpers ---


def _base_payload(
    request: SchedulerConfirmationVerificationRequest,
) -> dict[str, Any]:
    selector: dict[str, Any]
    if request.confirmation_id is not None:
        selector = {"kind": "confirmation_id", "value": request.confirmation_id}
    elif request.confirmation_artifact_path is not None:
        selector = {
            "kind": "confirmation_artifact_path",
            "value": str(request.confirmation_artifact_path),
        }
    elif request.latest:
        selector = {"kind": "latest", "value": None}
    else:
        selector = {"kind": "none", "value": None}

    return {
        "ok": False,
        "status": None,
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "source": VERIFIER_SOURCE,
        "selector": selector,
        "db_path": str(request.db_path),
        "artifact_root": (
            str(request.artifact_root) if request.artifact_root else None
        ),
        "confirmation_artifact_path": None,
        "confirmation_id": None,
        "confirmation_schema_version": None,
        "confirmation_created_at": None,
        "proposal_id": None,
        "proposal_hash": None,
        "proposal_artifact_path": None,
        "requested_proposal_item_id": request.proposal_item_id,
        "proposal_item_id": None,
        "item_hash": None,
        "task_key": None,
        "recommended_command_kind": None,
        "expected_command_kind": request.expected_command_kind,
        "expected_task_key": request.task_key,
        "max_age_minutes_override": request.max_age_minutes,
        "allowed_to_attempt": False,
        "execution_performed": False,
        "action_evidence_created": False,
        "checks": [],
        "expiration": None,
        "revalidation": None,
        "safety": dict(VERIFIER_SAFETY_FLAGS),
    }


def _finalize(
    base: dict[str, Any],
    *,
    status: str,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    if status not in VERIFICATION_STATUSES:
        raise SchedulerConfirmationVerifierError(
            f"unknown verification status: {status!r}"
        )
    base["status"] = status
    base["checks"] = checks
    base["ok"] = status != STATUS_NOT_FOUND
    base["allowed_to_attempt"] = status == STATUS_VALID
    # Mutation flags are always false; reassert here so any future
    # accidental edit gets caught by tests.
    base["execution_performed"] = False
    base["action_evidence_created"] = False
    base["safety"] = dict(VERIFIER_SAFETY_FLAGS)
    return base


def _locate_confirmation_artifact(
    request: SchedulerConfirmationVerificationRequest,
) -> Path | None:
    if request.confirmation_artifact_path is not None:
        return request.confirmation_artifact_path
    if request.confirmation_id is not None:
        return _find_confirmation_by_id(
            request.db_path, request.confirmation_id
        )
    if request.latest:
        return _find_latest_confirmation(request.db_path)
    return None


def _find_confirmation_by_id(db_path: Path, confirmation_id: str) -> Path | None:
    pattern = (
        f"%/scheduler_confirmations/{confirmation_id}/scheduler_confirmation.json"
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT path
            FROM task_artifacts
            WHERE artifact_type = ?
              AND path LIKE ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (CONFIRMATION_ARTIFACT_TYPE, pattern),
        ).fetchone()
    return Path(row["path"]) if row else None


def _find_latest_confirmation(db_path: Path) -> Path | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT path
            FROM task_artifacts
            WHERE artifact_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (CONFIRMATION_ARTIFACT_TYPE,),
        ).fetchone()
    return Path(row["path"]) if row else None


def _absorb_confirmation_metadata(
    base: dict[str, Any], on_disk: dict[str, Any]
) -> None:
    if isinstance(on_disk.get("confirmation_id"), str):
        base["confirmation_id"] = on_disk["confirmation_id"]
    if isinstance(on_disk.get("schema_version"), str):
        base["confirmation_schema_version"] = on_disk["schema_version"]
    if isinstance(on_disk.get("created_at"), str):
        base["confirmation_created_at"] = on_disk["created_at"]

    proposal = on_disk.get("proposal") or {}
    if isinstance(proposal, dict):
        if isinstance(proposal.get("proposal_id"), str):
            base["proposal_id"] = proposal["proposal_id"]
        if isinstance(proposal.get("proposal_hash"), str):
            base["proposal_hash"] = proposal["proposal_hash"]
        if isinstance(proposal.get("proposal_artifact_path"), str):
            base["proposal_artifact_path"] = proposal["proposal_artifact_path"]


def _absorb_item_metadata(
    base: dict[str, Any], selected_item: dict[str, Any]
) -> None:
    base["proposal_item_id"] = selected_item.get("proposal_item_id")
    base["item_hash"] = selected_item.get("item_hash")
    base["task_key"] = selected_item.get("task_key")
    base["recommended_command_kind"] = selected_item.get(
        "recommended_command_kind"
    )


def _schema_error(on_disk: dict[str, Any]) -> str | None:
    schema = on_disk.get("schema_version")
    if schema != CONFIRMATION_SCHEMA_VERSION:
        return (
            f"schema_version must be {CONFIRMATION_SCHEMA_VERSION!r}; "
            f"got {schema!r}"
        )
    if not isinstance(on_disk.get("confirmation_id"), str):
        return "confirmation_id must be a string"
    if not isinstance(on_disk.get("selected_items"), list):
        return "selected_items must be a list"
    if not isinstance(on_disk.get("safety"), dict):
        return "safety must be an object"
    proposal = on_disk.get("proposal")
    if not isinstance(proposal, dict):
        return "proposal must be an object"
    if not isinstance(proposal.get("proposal_id"), str):
        return "proposal.proposal_id must be a string"
    if not isinstance(proposal.get("proposal_hash"), str):
        return "proposal.proposal_hash must be a string"
    return None


def _confirmation_safety_error(on_disk: dict[str, Any]) -> str | None:
    safety = on_disk.get("safety") or {}
    if safety.get("execution_allowed"):
        return "safety.execution_allowed must be false"
    if safety.get("workflow_action_performed"):
        return "safety.workflow_action_performed must be false"
    if safety.get("action_evidence_created"):
        return "safety.action_evidence_created must be false"
    if safety.get("github_mutated"):
        return "safety.github_mutated must be false"
    if safety.get("merged"):
        return "safety.merged must be false"
    if safety.get("branch_pushed"):
        return "safety.branch_pushed must be false"
    if safety.get("pr_created"):
        return "safety.pr_created must be false"
    if safety.get("cleanup_performed"):
        return "safety.cleanup_performed must be false"
    if safety.get("task_status_changed"):
        return "safety.task_status_changed must be false"
    if safety.get("background_worker_started"):
        return "safety.background_worker_started must be false"
    return None


def _select_item(
    on_disk: dict[str, Any], proposal_item_id: str
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for item in on_disk.get("selected_items") or []:
        if (
            isinstance(item, dict)
            and item.get("proposal_item_id") == proposal_item_id
        ):
            matches.append(item)
    if len(matches) != 1:
        return None
    return matches[0]


def _item_field_error(selected_item: dict[str, Any]) -> str | None:
    if not isinstance(selected_item.get("item_hash"), str):
        return "selected item must include item_hash"
    if not isinstance(selected_item.get("task_key"), str):
        return "selected item must include task_key"
    if not isinstance(selected_item.get("recommended_command_kind"), str):
        return "selected item must include recommended_command_kind"
    if selected_item.get("execution_allowed", False):
        return "selected item must declare execution_allowed=false"
    if not selected_item.get("command_specific_confirmation_required", False):
        return (
            "selected item must declare command_specific_confirmation_required=true"
        )
    if not selected_item.get("revalidation_required", False):
        return "selected item must declare revalidation_required=true"
    return None


def _check_expiration(
    *,
    on_disk_created_at: Any,
    kind: str,
    max_age_minutes_override: int | None,
    now: datetime | None,
) -> dict[str, Any]:
    minutes = (
        max_age_minutes_override
        if max_age_minutes_override is not None
        else DEFAULT_EXPIRATION_MINUTES.get(kind)
    )
    report: dict[str, Any] = {
        "kind": kind,
        "max_age_minutes": minutes,
        "max_age_source": (
            "override" if max_age_minutes_override is not None else "default"
        ),
        "confirmation_created_at": (
            on_disk_created_at if isinstance(on_disk_created_at, str) else None
        ),
        "now": None,
        "age_seconds": None,
        "expired": True,
        "detail": None,
    }

    if minutes is None:
        report["detail"] = (
            f"no expiration policy configured for command kind {kind!r}"
        )
        return report

    if not isinstance(on_disk_created_at, str):
        report["detail"] = "confirmation.created_at is missing"
        return report

    try:
        created = _parse_iso8601(on_disk_created_at)
    except ValueError as exc:
        report["detail"] = f"could not parse created_at: {exc}"
        return report

    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - created).total_seconds()
    report["now"] = current.isoformat().replace("+00:00", "Z")
    report["age_seconds"] = age
    report["expired"] = age > minutes * 60
    if report["expired"]:
        report["detail"] = (
            f"confirmation is {age:.1f}s old; max age is {minutes * 60}s"
        )
    return report


def _parse_iso8601(value: str) -> datetime:
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _revalidate_current_state(
    *,
    db_path: Path,
    selected_item: dict[str, Any],
) -> dict[str, Any]:
    task_key = selected_item["task_key"]
    expected_kind = selected_item["recommended_command_kind"]
    expected_status = selected_item.get("expected_status")
    expected_phase_label = selected_item.get("expected_phase_label")
    expected_item_hash = selected_item["item_hash"]
    confirmed_warnings = list(selected_item.get("consistency_warnings") or [])

    report: dict[str, Any] = {
        "task_exists": False,
        "task_status_matches_expected": False,
        "current_phase_label_matches_expected": False,
        "current_recommendation_kind_matches": False,
        "current_item_hash_recomputed": False,
        "current_item_hash_matches": False,
        "current_item_hash": None,
        "current_status": None,
        "current_phase_label": None,
        "current_recommendation_kind": None,
        "current_consistency_warnings": [],
        "warnings_acceptable": False,
    }
    checks: list[dict[str, Any]] = []

    try:
        rec_payload = list_task_recommendations(
            TaskRecommendationsRequest(
                db_path=db_path,
                task_key=task_key,
                completed_limit=20,
            )
        )
    except TaskRecommendationsError as exc:
        checks.append(
            {
                "name": "current_recommendation_readable",
                "passed": False,
                "detail": f"could not read recommendations: {exc}",
            }
        )
        return {"report": report, "checks": checks}

    items = [
        item
        for item in (rec_payload.get("items") or [])
        if isinstance(item, dict) and item.get("task_key") == task_key
    ]
    if not items:
        checks.append(
            {
                "name": "task_exists_in_current_recommendations",
                "passed": False,
                "detail": f"no current recommendation for task_key {task_key!r}",
            }
        )
        return {"report": report, "checks": checks}

    rec_item = items[0]
    report["task_exists"] = True
    report["current_status"] = rec_item.get("status")
    report["current_phase_label"] = rec_item.get("current_phase_label")
    report["current_recommendation_kind"] = rec_item.get(
        "recommended_command_kind"
    )
    report["current_consistency_warnings"] = list(
        rec_item.get("consistency_warnings") or []
    )

    checks.append(
        {
            "name": "task_exists_in_current_recommendations",
            "passed": True,
            "detail": None,
        }
    )

    status_ok = (
        expected_status is None or rec_item.get("status") == expected_status
    )
    report["task_status_matches_expected"] = status_ok
    checks.append(
        {
            "name": "task_status_matches_expected",
            "passed": status_ok,
            "detail": (
                None
                if status_ok
                else (
                    f"expected status {expected_status!r}, current is "
                    f"{rec_item.get('status')!r}"
                )
            ),
        }
    )
    if not status_ok:
        return {"report": report, "checks": checks}

    phase_ok = (
        expected_phase_label is None
        or rec_item.get("current_phase_label") == expected_phase_label
    )
    report["current_phase_label_matches_expected"] = phase_ok
    checks.append(
        {
            "name": "current_phase_label_matches_expected",
            "passed": phase_ok,
            "detail": (
                None
                if phase_ok
                else (
                    f"expected phase {expected_phase_label!r}, current is "
                    f"{rec_item.get('current_phase_label')!r}"
                )
            ),
        }
    )
    if not phase_ok:
        return {"report": report, "checks": checks}

    kind_ok = rec_item.get("recommended_command_kind") == expected_kind
    report["current_recommendation_kind_matches"] = kind_ok
    checks.append(
        {
            "name": "current_recommendation_kind_matches",
            "passed": kind_ok,
            "detail": (
                None
                if kind_ok
                else (
                    f"expected kind {expected_kind!r}, current is "
                    f"{rec_item.get('recommended_command_kind')!r}"
                )
            ),
        }
    )
    if not kind_ok:
        return {"report": report, "checks": checks}

    current_warnings = report["current_consistency_warnings"]
    if confirmed_warnings:
        warnings_acceptable = (
            list(current_warnings) == list(confirmed_warnings)
        )
        report["warnings_acceptable"] = warnings_acceptable
        checks.append(
            {
                "name": "consistency_warnings_match_confirmation",
                "passed": warnings_acceptable,
                "detail": (
                    None
                    if warnings_acceptable
                    else (
                        f"current warnings {current_warnings!r} differ from "
                        f"acknowledged {confirmed_warnings!r}"
                    )
                ),
            }
        )
        if not warnings_acceptable:
            return {"report": report, "checks": checks}
    else:
        warnings_acceptable = not current_warnings
        report["warnings_acceptable"] = warnings_acceptable
        checks.append(
            {
                "name": "no_unacknowledged_consistency_warnings",
                "passed": warnings_acceptable,
                "detail": (
                    None
                    if warnings_acceptable
                    else f"new warnings appeared: {current_warnings!r}"
                ),
            }
        )
        if not warnings_acceptable:
            return {"report": report, "checks": checks}

    candidate = build_proposal_candidate(
        _rec_item_without_scheduler_meta_artifacts(rec_item)
    )
    if candidate is None:
        checks.append(
            {
                "name": "current_item_hash_recomputed",
                "passed": False,
                "detail": (
                    "current recommendation does not produce a proposal "
                    "candidate under the default policy"
                ),
            }
        )
        return {"report": report, "checks": checks}

    try:
        current_hash = compute_item_hash(candidate)
    except KeyError as exc:
        checks.append(
            {
                "name": "current_item_hash_recomputed",
                "passed": False,
                "detail": f"could not recompute item_hash: {exc}",
            }
        )
        return {"report": report, "checks": checks}

    report["current_item_hash_recomputed"] = True
    report["current_item_hash"] = current_hash
    matches = current_hash == expected_item_hash
    report["current_item_hash_matches"] = matches
    checks.append(
        {
            "name": "current_item_hash_matches",
            "passed": matches,
            "detail": (
                None
                if matches
                else (
                    f"current item_hash {current_hash} differs from "
                    f"confirmed {expected_item_hash}"
                )
            ),
        }
    )
    return {"report": report, "checks": checks}


_SCHEDULER_META_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {PROPOSAL_ARTIFACT_TYPE, CONFIRMATION_ARTIFACT_TYPE}
)


def _rec_item_without_scheduler_meta_artifacts(
    rec_item: dict[str, Any],
) -> dict[str, Any]:
    """Return a shallow copy of ``rec_item`` with scheduler bookkeeping
    artifacts (``scheduler_proposal`` / ``scheduler_confirmation``)
    removed from ``related_artifacts``.

    Those artifact types are written by the proposal and confirmation
    surfaces themselves, so any rec_item observed *after* a proposal
    and/or confirmation was recorded will include them even though they
    are not workflow-action evidence. Their presence would otherwise
    perturb ``item_hash`` recomputation in a way that does not reflect
    real workflow state change.
    """

    related = [
        artifact
        for artifact in (rec_item.get("related_artifacts") or [])
        if isinstance(artifact, dict)
        and artifact.get("artifact_type") not in _SCHEDULER_META_ARTIFACT_TYPES
    ]
    cleaned = dict(rec_item)
    cleaned["related_artifacts"] = related
    return cleaned


__all__ = [
    "DEFAULT_EXPIRATION_MINUTES",
    "STATUS_BLOCKED",
    "STATUS_INVALID",
    "STATUS_NOT_FOUND",
    "STATUS_VALID",
    "VERIFICATION_SCHEMA_VERSION",
    "VERIFICATION_STATUSES",
    "VERIFIER_SAFETY_FLAGS",
    "VERIFIER_SOURCE",
    "SchedulerConfirmationVerificationRequest",
    "SchedulerConfirmationVerifierError",
    "verify_scheduler_confirmation_item",
]
