"""Read-only review surface for scheduler proposal artifacts.

This module loads scheduler proposal artifacts recorded by
``scheduler_proposals.py`` and produces a review payload with hash
verification and safety status. It is NOT a confirmation surface, NOT an
execution surface, and does NOT mutate any workflow state.

A review payload is never action evidence. It must not be interpreted as:

- a confirmation artifact,
- permission to execute a proposed action,
- proof that a proposed action ran,
- branch push / PR / merge / cleanup evidence.

The review surface only reads:

- SQLite ``task_artifacts`` rows of type ``scheduler_proposal`` to discover
  candidate proposal artifact paths.
- The on-disk ``scheduler_proposal.json`` files those rows point to.

It performs no writes, opens no subprocesses, and contacts no remote
service.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.scheduler_proposals import (
    HASH_ALGORITHM,
    ITEM_HASH_PAYLOAD_VERSION,
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_HASH_PAYLOAD_VERSION,
    SCHEMA_VERSION,
    verify_proposal_hashes,
)


REVIEW_SCHEMA_VERSION = "scheduler_proposal_review.v1"
REVIEW_SOURCE = "scheduler_proposal_review"
DEFAULT_LIST_LIMIT = 50

REVIEW_STATUSES: tuple[str, ...] = (
    "valid",
    "invalid_hash",
    "missing_artifact",
    "unreadable_artifact",
    "unsupported_schema",
    "unsafe_payload",
)

REVIEW_SAFETY_FLAGS: dict[str, bool] = {
    "read_only": True,
    "proposal_only": True,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_approve": False,
    "will_cleanup": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_db": False,
    "will_mutate_github": False,
    "will_start_background_worker": False,
}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PROPOSAL_ID = re.compile(r"^proposal-[0-9A-Za-z._\-]+$")


class SchedulerProposalReviewError(RuntimeError):
    """Raised when a review request cannot be honored."""


@dataclass(frozen=True)
class SchedulerProposalReviewRequest:
    """Inputs to a scheduler proposal review.

    ``review_scheduler_proposal`` requires exactly one of ``artifact_path``,
    ``proposal_id``, or ``latest=True``. ``list_scheduler_proposals`` ignores
    those fields.

    All paths must be absolute. ``artifact_root``, when provided, is
    informational: it does not restrict where artifacts may be read from
    (the DB row is authoritative), but the review payload includes it for
    audit.
    """

    db_path: Path
    artifact_root: Path | None = None
    proposal_id: str | None = None
    artifact_path: Path | None = None
    latest: bool = False
    include_items: bool = True
    verify_hashes: bool = True
    list_limit: int = DEFAULT_LIST_LIMIT

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

        if self.artifact_path is not None:
            ap = Path(self.artifact_path).expanduser()
            if not ap.is_absolute():
                raise ValueError("artifact_path must be an absolute path")
            object.__setattr__(self, "artifact_path", ap)

        if self.proposal_id is not None:
            pid = self.proposal_id.strip()
            if not pid:
                raise ValueError("proposal_id must not be empty")
            if not _PROPOSAL_ID.match(pid):
                raise ValueError(f"invalid proposal_id format: {pid!r}")
            object.__setattr__(self, "proposal_id", pid)

        if self.list_limit < 0:
            raise ValueError("list_limit must be zero or positive")


def list_scheduler_proposals(
    request: SchedulerProposalReviewRequest,
) -> dict[str, Any]:
    """Return summaries of recorded scheduler proposal artifacts.

    Reads the SQLite ``task_artifacts`` table for rows of type
    ``scheduler_proposal``, groups them by artifact path (each proposal
    artifact is recorded once per selected task), and attempts to read each
    on-disk file to extract proposal-level identifiers. The list does not
    perform full hash verification (use :func:`review_scheduler_proposal`
    for that).
    """

    _require_db(request.db_path)
    rows = _query_proposal_artifact_groups(request.db_path)

    proposals: list[dict[str, Any]] = []
    for group in rows[: request.list_limit]:
        proposals.append(_summary_for_group(group))

    return {
        "ok": True,
        "review_mode": "list",
        "schema_version": REVIEW_SCHEMA_VERSION,
        "source": REVIEW_SOURCE,
        "proposal_count": len(proposals),
        "list_limit": request.list_limit,
        "total_recorded": len(rows),
        "proposals": proposals,
        "safety": dict(REVIEW_SAFETY_FLAGS),
    }


def review_scheduler_proposal(
    request: SchedulerProposalReviewRequest,
) -> dict[str, Any]:
    """Load and review a single scheduler proposal artifact."""

    _require_db(request.db_path)

    selectors = sum(
        1
        for selector in (
            request.artifact_path is not None,
            request.proposal_id is not None,
            request.latest,
        )
        if selector
    )
    if selectors == 0:
        raise SchedulerProposalReviewError(
            "review requires one of artifact_path, proposal_id, or latest=True"
        )
    if selectors > 1:
        raise SchedulerProposalReviewError(
            "review accepts only one of artifact_path, proposal_id, latest"
        )

    if request.artifact_path is not None:
        artifact_path: Path | None = request.artifact_path
        db_task_keys = _task_keys_for_path(request.db_path, artifact_path)
        resolved_proposal_id: str | None = _proposal_id_from_path(artifact_path)
    elif request.proposal_id is not None:
        artifact_path, db_task_keys = _find_proposal_by_id(
            request.db_path, request.proposal_id
        )
        resolved_proposal_id = request.proposal_id
    else:
        artifact_path, db_task_keys = _find_latest_proposal(request.db_path)
        resolved_proposal_id = (
            _proposal_id_from_path(artifact_path) if artifact_path else None
        )

    base = _review_base(
        request=request,
        artifact_path=artifact_path,
        proposal_id=resolved_proposal_id,
        db_task_keys=db_task_keys,
    )

    if artifact_path is None:
        return _finalize_review(
            base,
            review_status="missing_artifact",
            error="No scheduler_proposal artifact recorded for the requested selector.",
        )

    if not artifact_path.exists():
        return _finalize_review(
            base,
            review_status="missing_artifact",
            error=f"Scheduler proposal artifact file not found on disk: {artifact_path}",
        )

    try:
        raw = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _finalize_review(
            base,
            review_status="unreadable_artifact",
            error=f"Could not read artifact: {exc}",
        )

    try:
        on_disk = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _finalize_review(
            base,
            review_status="unreadable_artifact",
            error=f"Could not parse artifact JSON: {exc}",
        )

    if not isinstance(on_disk, dict):
        return _finalize_review(
            base,
            review_status="unreadable_artifact",
            error="Artifact JSON root is not an object.",
        )

    base["on_disk"] = on_disk
    if isinstance(on_disk.get("proposal_id"), str):
        base["proposal_id"] = on_disk["proposal_id"]
    if isinstance(on_disk.get("schema_version"), str):
        base["schema_version_on_disk"] = on_disk["schema_version"]

    schema_error = _schema_error(on_disk)
    if schema_error is not None:
        return _finalize_review(
            base,
            review_status="unsupported_schema",
            error=schema_error,
        )

    safety_error = _safety_error(on_disk)
    if safety_error is not None:
        return _finalize_review(
            base,
            review_status="unsafe_payload",
            error=safety_error,
        )

    hash_report: dict[str, Any] | None = None
    hash_valid = True
    if request.verify_hashes:
        hash_report = verify_proposal_hashes(on_disk)
        hash_valid = bool(hash_report["proposal_hash_valid"]) and all(
            bool(report["item_hash_valid"]) for report in hash_report["items"]
        )
        if not hash_valid:
            base["hash_report"] = hash_report
            return _finalize_review(
                base,
                review_status="invalid_hash",
                error="Hash mismatch on proposal or one or more items.",
            )

    if hash_report is not None:
        base["hash_report"] = hash_report

    base["hash_valid"] = hash_valid
    return _finalize_review(base, review_status="valid")


# --- Internal helpers ---


def _require_db(db_path: Path) -> None:
    if not db_path.exists():
        raise SchedulerProposalReviewError(
            f"SQLite state DB not found: {db_path}"
        )


def _query_proposal_artifact_groups(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT MAX(id) AS last_id,
                   MIN(id) AS first_id,
                   path,
                   MIN(created_at) AS first_created_at,
                   MAX(created_at) AS last_created_at,
                   COUNT(*) AS row_count
            FROM task_artifacts
            WHERE artifact_type = ?
            GROUP BY path
            ORDER BY last_id DESC
            """,
            (PROPOSAL_ARTIFACT_TYPE,),
        ).fetchall()

    groups: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            key_rows = conn.execute(
                """
                SELECT DISTINCT task_key
                FROM task_artifacts
                WHERE artifact_type = ? AND path = ?
                ORDER BY task_key
                """,
                (PROPOSAL_ARTIFACT_TYPE, path),
            ).fetchall()
        groups.append(
            {
                "last_id": row["last_id"],
                "first_id": row["first_id"],
                "path": path,
                "first_recorded_at": row["first_created_at"],
                "last_recorded_at": row["last_created_at"],
                "row_count": row["row_count"],
                "task_keys": [r["task_key"] for r in key_rows],
            }
        )
    return groups


def _summary_for_group(group: dict[str, Any]) -> dict[str, Any]:
    path = Path(group["path"])
    proposal_id = _proposal_id_from_path(path)
    summary: dict[str, Any] = {
        "proposal_id": proposal_id,
        "artifact_path": str(path),
        "first_recorded_at": group["first_recorded_at"],
        "last_recorded_at": group["last_recorded_at"],
        "task_keys": list(group["task_keys"]),
        "task_key_count": len(group["task_keys"]),
        "on_disk_ok": False,
        "on_disk_error": None,
        "proposal_hash": None,
        "schema_version": None,
        "mode": None,
        "created_at": None,
        "item_count": None,
        "review_status": "missing_artifact",
    }

    if not path.exists():
        summary["on_disk_error"] = "artifact file not found"
        return summary

    try:
        raw = path.read_text(encoding="utf-8")
        on_disk = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        summary["on_disk_error"] = f"could not read artifact: {exc}"
        summary["review_status"] = "unreadable_artifact"
        return summary

    if not isinstance(on_disk, dict):
        summary["on_disk_error"] = "artifact JSON root is not an object"
        summary["review_status"] = "unreadable_artifact"
        return summary

    summary["on_disk_ok"] = True
    summary["proposal_id"] = on_disk.get("proposal_id") or proposal_id
    summary["proposal_hash"] = on_disk.get("proposal_hash")
    summary["schema_version"] = on_disk.get("schema_version")
    summary["mode"] = on_disk.get("mode")
    summary["created_at"] = on_disk.get("created_at")
    items = on_disk.get("items")
    if isinstance(items, list):
        summary["item_count"] = len(items)

    schema_error = _schema_error(on_disk)
    if schema_error is not None:
        summary["review_status"] = "unsupported_schema"
        summary["on_disk_error"] = schema_error
        return summary

    safety_error = _safety_error(on_disk)
    if safety_error is not None:
        summary["review_status"] = "unsafe_payload"
        summary["on_disk_error"] = safety_error
        return summary

    summary["review_status"] = "valid_unverified"
    return summary


def _proposal_id_from_path(path: Path | None) -> str | None:
    if path is None:
        return None
    parent = path.parent.name
    if _PROPOSAL_ID.match(parent):
        return parent
    return None


def _task_keys_for_path(db_path: Path, artifact_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT task_key
            FROM task_artifacts
            WHERE artifact_type = ? AND path = ?
            ORDER BY task_key
            """,
            (PROPOSAL_ARTIFACT_TYPE, str(artifact_path)),
        ).fetchall()
    return [row["task_key"] for row in rows]


def _find_proposal_by_id(
    db_path: Path, proposal_id: str
) -> tuple[Path | None, list[str]]:
    pattern = f"%/scheduler_proposals/{proposal_id}/scheduler_proposal.json"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT path, task_key
            FROM task_artifacts
            WHERE artifact_type = ?
              AND path LIKE ?
            ORDER BY id ASC
            """,
            (PROPOSAL_ARTIFACT_TYPE, pattern),
        ).fetchall()
    if not rows:
        return (None, [])
    path = Path(rows[0]["path"])
    task_keys = sorted({row["task_key"] for row in rows})
    return (path, task_keys)


def _find_latest_proposal(db_path: Path) -> tuple[Path | None, list[str]]:
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
            (PROPOSAL_ARTIFACT_TYPE,),
        ).fetchone()
    if row is None:
        return (None, [])
    path = Path(row["path"])
    return (path, _task_keys_for_path(db_path, path))


def _schema_error(on_disk: dict[str, Any]) -> str | None:
    if on_disk.get("schema_version") != SCHEMA_VERSION:
        return f"schema_version must be {SCHEMA_VERSION!r}"
    if not isinstance(on_disk.get("proposal_id"), str) or not on_disk["proposal_id"]:
        return "proposal_id must be a non-empty string"
    if not _is_sha256_hex(on_disk.get("proposal_hash")):
        return "proposal_hash must be a sha256 hex string"
    if on_disk.get("hash_algorithm") != HASH_ALGORITHM:
        return f"hash_algorithm must be {HASH_ALGORITHM!r}"
    if (
        on_disk.get("proposal_hash_payload_version")
        != PROPOSAL_HASH_PAYLOAD_VERSION
    ):
        return (
            f"proposal_hash_payload_version must be "
            f"{PROPOSAL_HASH_PAYLOAD_VERSION!r}"
        )
    if on_disk.get("item_hash_payload_version") != ITEM_HASH_PAYLOAD_VERSION:
        return (
            f"item_hash_payload_version must be {ITEM_HASH_PAYLOAD_VERSION!r}"
        )
    items = on_disk.get("items")
    if not isinstance(items, list):
        return "items must be a list"
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return f"items[{index}] must be an object"
        if not isinstance(item.get("proposal_item_id"), str) or not item[
            "proposal_item_id"
        ]:
            return f"items[{index}].proposal_item_id must be a non-empty string"
        if not _is_sha256_hex(item.get("item_hash")):
            return f"items[{index}].item_hash must be a sha256 hex string"
    if not isinstance(on_disk.get("safety"), dict):
        return "safety must be an object"
    return None


def _safety_error(on_disk: dict[str, Any]) -> str | None:
    safety = on_disk.get("safety") or {}
    if not safety.get("proposal_only"):
        return "safety.proposal_only must be true"
    if safety.get("workflow_action_performed"):
        return "safety.workflow_action_performed must be false"
    if safety.get("action_evidence_created"):
        return "safety.action_evidence_created must be false"
    if safety.get("executor_started"):
        return "safety.executor_started must be false"
    if safety.get("merged"):
        return "safety.merged must be false"
    if safety.get("branch_pushed"):
        return "safety.branch_pushed must be false"
    if safety.get("pr_created"):
        return "safety.pr_created must be false"
    if safety.get("cleanup_performed"):
        return "safety.cleanup_performed must be false"
    if safety.get("background_worker_started"):
        return "safety.background_worker_started must be false"
    return None


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_HEX.match(value))


def _review_base(
    *,
    request: SchedulerProposalReviewRequest,
    artifact_path: Path | None,
    proposal_id: str | None,
    db_task_keys: list[str],
) -> dict[str, Any]:
    return {
        "ok": False,
        "review_mode": "single",
        "review_status": None,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "source": REVIEW_SOURCE,
        "proposal_id": proposal_id,
        "proposal_hash": None,
        "hash_valid": None,
        "schema_version_on_disk": None,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "db_task_keys": list(db_task_keys),
        "selector": _selector_used(request),
        "verify_hashes": request.verify_hashes,
        "include_items": request.include_items,
        "artifact_root": str(request.artifact_root) if request.artifact_root else None,
        "db_path": str(request.db_path),
        "safety": dict(REVIEW_SAFETY_FLAGS),
        "on_disk": None,
        "hash_report": None,
        "error": None,
    }


def _selector_used(request: SchedulerProposalReviewRequest) -> dict[str, Any]:
    if request.artifact_path is not None:
        return {"kind": "artifact_path", "value": str(request.artifact_path)}
    if request.proposal_id is not None:
        return {"kind": "proposal_id", "value": request.proposal_id}
    return {"kind": "latest", "value": None}


def _finalize_review(
    base: dict[str, Any],
    *,
    review_status: str,
    error: str | None = None,
) -> dict[str, Any]:
    if review_status not in REVIEW_STATUSES:
        raise SchedulerProposalReviewError(
            f"unknown review_status: {review_status!r}"
        )
    base["review_status"] = review_status
    base["error"] = error
    on_disk = base.get("on_disk")

    if isinstance(on_disk, dict):
        base["proposal_hash"] = on_disk.get("proposal_hash")
        summary = on_disk.get("summary")
        if isinstance(summary, dict):
            base["proposal_summary"] = {
                "item_count": summary.get("item_count"),
                "candidate_count": summary.get("candidate_count"),
                "executable_count": summary.get("executable_count"),
                "warning_count": summary.get("warning_count"),
                "requires_human_review": summary.get("requires_human_review"),
                "requires_explicit_confirmation_before_execution": summary.get(
                    "requires_explicit_confirmation_before_execution"
                ),
                "mutation_performed": summary.get("mutation_performed"),
                "proposal_evidence_recorded": summary.get(
                    "proposal_evidence_recorded"
                ),
            }
        base["mode"] = on_disk.get("mode")
        base["created_at"] = on_disk.get("created_at")
        base["filters"] = on_disk.get("filters")
        base["policy"] = on_disk.get("policy")
        base["proposal_safety"] = on_disk.get("safety")
        if base["include_items"]:
            base["items"] = [
                _item_view(item, base.get("hash_report"))
                for item in (on_disk.get("items") or [])
                if isinstance(item, dict)
            ]
        else:
            base["items"] = None
    else:
        base["proposal_summary"] = None
        base["mode"] = None
        base["created_at"] = None
        base["filters"] = None
        base["policy"] = None
        base["proposal_safety"] = None
        base["items"] = None

    base["ok"] = review_status == "valid"
    base.pop("on_disk", None)
    return base


def _item_view(
    item: dict[str, Any],
    hash_report: dict[str, Any] | None,
) -> dict[str, Any]:
    item_hash_valid: bool | None = None
    if isinstance(hash_report, dict):
        for entry in hash_report.get("items") or []:
            if (
                isinstance(entry, dict)
                and entry.get("proposal_item_id") == item.get("proposal_item_id")
            ):
                item_hash_valid = bool(entry.get("item_hash_valid"))
                break

    return {
        "proposal_item_id": item.get("proposal_item_id"),
        "item_hash": item.get("item_hash"),
        "item_hash_valid": item_hash_valid,
        "task_key": item.get("task_key"),
        "project": item.get("project"),
        "title": item.get("title"),
        "status": item.get("status"),
        "expected_status": item.get("expected_status"),
        "current_phase_label": item.get("current_phase_label"),
        "expected_phase_label": item.get("expected_phase_label"),
        "recommended_command_kind": item.get("recommended_command_kind"),
        "proposed_action": item.get("proposed_action"),
        "reason": item.get("reason"),
        "severity": item.get("severity"),
        "confidence": item.get("confidence"),
        "executable": item.get("executable"),
        "requires_human_confirmation": item.get("requires_human_confirmation"),
        "consistency_warnings": list(item.get("consistency_warnings") or []),
        "missing_evidence": list(item.get("missing_evidence") or []),
        "expected_refs": item.get("expected_refs"),
        "expected_evidence_summary": item.get("expected_evidence_summary"),
        "priority_rank": item.get("priority_rank"),
        "safety_flags": item.get("safety_flags"),
    }


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "REVIEW_SAFETY_FLAGS",
    "REVIEW_SCHEMA_VERSION",
    "REVIEW_SOURCE",
    "REVIEW_STATUSES",
    "SchedulerProposalReviewError",
    "SchedulerProposalReviewRequest",
    "list_scheduler_proposals",
    "review_scheduler_proposal",
]
