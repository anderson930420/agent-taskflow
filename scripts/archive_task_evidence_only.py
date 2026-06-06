"""Operator-confirmed evidence-only / superseded task archive command.

This command provides a separate, explicit archive path for tasks that should
be closed out for **evidence-only** or **superseded** reasons, where the strict
merged-PR closeout pipeline does not (and should not) apply.

It is **not** merged-PR closeout. It does **not** replace
``scripts/confirm_task_closeout.py``. ``confirm_task_closeout.py`` is the
stricter command that requires verified draft PR pipeline evidence, local
cleanup evidence, remote branch cleanup evidence, and a merged GitHub PR before
it will mark a task ``completed``/``done``. Do not weaken that command and do
not route evidence-only/superseded tasks through it.

Use this command for tasks such as:

- a task that was manually salvaged by a separately reconstructed PR
  (``salvaged_by_pr``);
- smoke-only / no-op evidence tasks (``smoke_evidence_only``, ``no_op_evidence``);
- a smoke task that a later smoke run superseded
  (``superseded_by_later_smoke``);
- a task left behind by a stale policy block or stale branch push
  (``stale_policy_blocked``, ``stale_branch_push``);
- an obsolete queued task that should not be executed (``obsolete_queued``).

Safety boundaries (enforced by this command):

- It is **dry-run by default**. It performs **no DB write** unless
  ``--confirm-evidence-archive`` is present.
- It never calls GitHub, never closes an issue, never merges, never creates a
  PR, never pushes, never deletes a local or remote branch, never inspects or
  removes filesystem worktrees, never runs an executor or validator, never adds
  automation, a scheduler loop, a background worker, a webhook, or a polling
  loop, and never modifies cron/systemd/nginx/deploy configuration.

Human review remains the final gate.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord, utc_now_iso, validate_task_status
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key


ARTIFACT_TYPE = "task_evidence_archive"
EVENT_TYPE = "task_evidence_archived"
SOURCE = "archive_task_evidence_only"
CONFIRM_FLAG = "--confirm-evidence-archive"
DEFAULT_TARGET_STATUS = "archived"

# Explicit reason codes for an evidence-only / superseded archive. Keep these
# stable; docs and operator runbooks reference them directly.
REASON_CODES: tuple[str, ...] = (
    "salvaged_by_pr",
    "smoke_evidence_only",
    "superseded_by_later_smoke",
    "no_op_evidence",
    "stale_policy_blocked",
    "stale_branch_push",
    "obsolete_queued",
)

# Statuses that are already terminal: an evidence-only archive is for
# non-terminal salvage/superseded work, not for re-archiving finished tasks.
INELIGIBLE_STATUSES: frozenset[str] = frozenset(
    {"completed", "done", "canceled", "archived"}
)

# Target statuses an evidence-only archive may write. ``archived`` is the
# default; the others allow an operator to record a superseded/cancelled
# disposition without inventing new automation.
ARCHIVE_TARGET_STATUSES: frozenset[str] = frozenset(
    {"archived", "canceled", "cleaned", "rejected"}
)


class EvidenceArchiveError(RuntimeError):
    """Raised when the evidence-only archive cannot proceed safely."""


@dataclass(frozen=True)
class EvidenceArchiveRequest:
    """Request for previewing or confirming an evidence-only task archive."""

    task_key: str
    reason_code: str
    db_path: Path | None = None
    artifact_root: Path | None = None
    note: str | None = None
    superseded_by_pr: str | None = None
    superseded_by_task: str | None = None
    target_status: str = DEFAULT_TARGET_STATUS
    dry_run: bool = False
    confirm_evidence_archive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "reason_code", _normalize_reason_code(self.reason_code))
        object.__setattr__(
            self, "target_status", _normalize_target_status(self.target_status)
        )
        if self.db_path is not None:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser())
        if self.artifact_root is not None:
            object.__setattr__(
                self, "artifact_root", Path(self.artifact_root).expanduser()
            )
        if self.superseded_by_task is not None:
            object.__setattr__(
                self,
                "superseded_by_task",
                normalize_task_key(self.superseded_by_task),
            )


@dataclass(frozen=True)
class EvidenceArchiveResult:
    """Structured evidence-only archive preview or confirmation result."""

    ok: bool
    status: str
    task_key: str
    reason_code: str
    task_status: str | None
    previous_task_status: str | None
    new_task_status: str | None
    note: str | None
    superseded_by_pr: str | None
    superseded_by_task: str | None
    evidence: dict[str, Any]
    summary: dict[str, Any]
    safety: dict[str, Any]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    warnings: list[str] = field(default_factory=list)
    blocking_warnings: list[str] = field(default_factory=list)
    performed: bool = False
    dry_run: bool = False
    confirmation_required: bool = True
    artifact_recorded: bool = False
    event_recorded: bool = False
    task_status_changed: bool = False
    db_written: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), sort_keys=True))


def archive_task_evidence_only(
    request: EvidenceArchiveRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> EvidenceArchiveResult:
    """Preview or confirm an evidence-only / superseded task archive.

    This never touches GitHub, branches, worktrees, executors, validators,
    cron, or any automation. It only reads the task, and on explicit
    confirmation updates the local task status and records archive evidence.
    """

    db_path = request.db_path or default_db_path()
    if not db_path.exists():
        return _not_found_result(
            request=request,
            error=f"SQLite state DB not found: {db_path}",
        )

    current_store = store or TaskMirrorStore(db_path)
    task = current_store.get_task(request.task_key)
    if task is None:
        return _not_found_result(
            request=request, error=f"Task not found: {request.task_key}"
        )

    warnings = _advisory_warnings(request)

    if task.status in INELIGIBLE_STATUSES:
        return _blocked_result(
            request=request,
            task=task,
            warnings=warnings,
            error=(
                f"Task {request.task_key} is already terminal with status "
                f"{task.status}; evidence-only archive applies to non-terminal "
                "salvage/superseded tasks only"
            ),
        )

    if request.dry_run:
        return _dry_run_result(request=request, task=task, warnings=warnings)

    if not request.confirm_evidence_archive:
        return _blocked_result(
            request=request,
            task=task,
            warnings=warnings,
            error=f"Missing required {CONFIRM_FLAG} flag",
        )

    previous_status = task.status
    target_status = request.target_status
    current_store.update_task_status(
        request.task_key,
        target_status,
        source=SOURCE,
        message=(
            f"Evidence-only archive confirmed (reason_code={request.reason_code})"
        ),
    )

    evidence_payload = _evidence_payload(
        request=request,
        previous_task_status=previous_status,
        new_task_status=target_status,
    )
    artifact_path = _write_evidence_artifact(
        task=task,
        artifact_root=request.artifact_root,
        artifact_payload=evidence_payload,
    )
    current_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, artifact_path)

    event_recorded = False
    if hasattr(current_store, "record_task_event"):
        current_store.record_task_event(
            request.task_key,
            EVENT_TYPE,
            SOURCE,
            message=(
                f"Task evidence-only archived (reason_code={request.reason_code})"
            ),
            payload=evidence_payload,
        )
        event_recorded = True

    updated_task = current_store.get_task(request.task_key)
    return _success_result(
        request=request,
        task=updated_task or task,
        previous_task_status=previous_status,
        new_task_status=target_status,
        artifact_path=artifact_path,
        event_recorded=event_recorded,
        warnings=warnings,
    )


def _advisory_warnings(request: EvidenceArchiveRequest) -> list[str]:
    """Return non-blocking advisory warnings for the request."""
    warnings: list[str] = []
    if request.reason_code == "salvaged_by_pr" and not request.superseded_by_pr:
        warnings.append(
            "reason_code salvaged_by_pr is best recorded with --superseded-by-pr"
        )
    if (
        request.reason_code == "superseded_by_later_smoke"
        and not request.superseded_by_task
    ):
        warnings.append(
            "reason_code superseded_by_later_smoke is best recorded with "
            "--superseded-by-task"
        )
    return warnings


def _evidence_payload(
    *,
    request: EvidenceArchiveRequest,
    previous_task_status: str,
    new_task_status: str,
) -> dict[str, Any]:
    """Build the deterministic evidence-only archive artifact payload."""
    return {
        "schema_version": "1",
        "artifact_type": ARTIFACT_TYPE,
        "kind": EVENT_TYPE,
        "task_key": request.task_key,
        "reason_code": request.reason_code,
        "note": request.note,
        "superseded_by_pr": request.superseded_by_pr,
        "superseded_by_task": request.superseded_by_task,
        "previous_task_status": previous_task_status,
        "new_task_status": new_task_status,
        "task_status_changed": previous_task_status != new_task_status,
        "archive_scope": "evidence_only",
        "is_merged_pr_closeout": False,
        "requires_human_confirmation": True,
        "confirmation_flag": CONFIRM_FLAG,
        "recorded_at": utc_now_iso(),
        "safety": _safety_block(
            human_confirmation_confirmed=True,
            db_written=True,
            task_status_changed=previous_task_status != new_task_status,
        ),
    }


def _write_evidence_artifact(
    *,
    task: TaskRecord,
    artifact_root: Path | None,
    artifact_payload: dict[str, Any],
) -> Path:
    output_root = _resolve_artifact_root(task, artifact_root)
    artifact_path = output_root / task.task_key / "task_evidence_archive.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _resolve_artifact_root(task: TaskRecord, artifact_root: Path | None) -> Path:
    if artifact_root is not None:
        return artifact_root / ARTIFACT_TYPE
    if task.artifact_dir is not None:
        return task.artifact_dir.resolve().parent / ARTIFACT_TYPE
    return task.repo_path / ".agent-taskflow" / "artifacts" / ARTIFACT_TYPE


def _success_result(
    *,
    request: EvidenceArchiveRequest,
    task: TaskRecord,
    previous_task_status: str,
    new_task_status: str,
    artifact_path: Path,
    event_recorded: bool,
    warnings: list[str],
) -> EvidenceArchiveResult:
    status_changed = previous_task_status != new_task_status
    return EvidenceArchiveResult(
        ok=True,
        status="task_evidence_archived",
        task_key=request.task_key,
        reason_code=request.reason_code,
        task_status=task.status,
        previous_task_status=previous_task_status,
        new_task_status=new_task_status,
        note=request.note,
        superseded_by_pr=request.superseded_by_pr,
        superseded_by_task=request.superseded_by_task,
        evidence={
            "artifact_recorded": True,
            "event_recorded": event_recorded,
            "artifact_type": ARTIFACT_TYPE,
            "event_type": EVENT_TYPE,
            "artifact_path": str(artifact_path),
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_confirmation": True,
            "confirmation_flag": CONFIRM_FLAG,
        },
        summary={
            "task_evidence_archived": True,
            "task_status_changed": status_changed,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_review": False,
        },
        safety=_safety_block(
            human_confirmation_confirmed=True,
            db_written=True,
            task_status_changed=status_changed,
        ),
        next_allowed_actions=[
            "retain the archived local task record for review",
            "optionally record a manual GitHub issue note in a later step",
        ],
        actions_not_performed=_actions_not_performed(),
        warnings=list(warnings),
        blocking_warnings=[],
        performed=True,
        dry_run=False,
        confirmation_required=True,
        artifact_recorded=True,
        event_recorded=event_recorded,
        task_status_changed=status_changed,
        db_written=True,
        error=None,
    )


def _dry_run_result(
    *,
    request: EvidenceArchiveRequest,
    task: TaskRecord,
    warnings: list[str],
) -> EvidenceArchiveResult:
    target_status = request.target_status
    return EvidenceArchiveResult(
        ok=True,
        status="dry_run",
        task_key=request.task_key,
        reason_code=request.reason_code,
        task_status=task.status,
        previous_task_status=task.status,
        new_task_status=target_status,
        note=request.note,
        superseded_by_pr=request.superseded_by_pr,
        superseded_by_task=request.superseded_by_task,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "artifact_type": ARTIFACT_TYPE,
            "event_type": EVENT_TYPE,
            "artifact_path": None,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_confirmation": True,
            "confirmation_flag": CONFIRM_FLAG,
        },
        summary={
            "task_evidence_archived": False,
            "task_status_changed": False,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_review": True,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            db_written=False,
            task_status_changed=False,
        ),
        next_allowed_actions=[
            f"run the archive again with {CONFIRM_FLAG} to apply",
        ],
        actions_not_performed=_actions_not_performed(include_state_changes=True),
        warnings=list(warnings),
        blocking_warnings=[],
        performed=False,
        dry_run=True,
        confirmation_required=True,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        error=None,
    )


def _blocked_result(
    *,
    request: EvidenceArchiveRequest,
    task: TaskRecord,
    warnings: list[str],
    error: str,
) -> EvidenceArchiveResult:
    return EvidenceArchiveResult(
        ok=False,
        status="blocked",
        task_key=request.task_key,
        reason_code=request.reason_code,
        task_status=task.status,
        previous_task_status=task.status,
        new_task_status=None,
        note=request.note,
        superseded_by_pr=request.superseded_by_pr,
        superseded_by_task=request.superseded_by_task,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "artifact_type": ARTIFACT_TYPE,
            "event_type": EVENT_TYPE,
            "artifact_path": None,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_confirmation": True,
            "confirmation_flag": CONFIRM_FLAG,
        },
        summary={
            "task_evidence_archived": False,
            "task_status_changed": False,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_review": True,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            db_written=False,
            task_status_changed=False,
        ),
        next_allowed_actions=[
            "resolve the blocking reason",
            f"rerun with {CONFIRM_FLAG} once the task is eligible",
        ],
        actions_not_performed=_actions_not_performed(include_state_changes=True),
        warnings=[*warnings, error],
        blocking_warnings=[error],
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        error=error,
    )


def _not_found_result(
    *,
    request: EvidenceArchiveRequest,
    error: str,
) -> EvidenceArchiveResult:
    return EvidenceArchiveResult(
        ok=False,
        status="not_found",
        task_key=request.task_key,
        reason_code=request.reason_code,
        task_status=None,
        previous_task_status=None,
        new_task_status=None,
        note=request.note,
        superseded_by_pr=request.superseded_by_pr,
        superseded_by_task=request.superseded_by_task,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "artifact_type": ARTIFACT_TYPE,
            "event_type": EVENT_TYPE,
            "artifact_path": None,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_confirmation": True,
            "confirmation_flag": CONFIRM_FLAG,
        },
        summary={
            "task_evidence_archived": False,
            "task_status_changed": False,
            "archive_scope": "evidence_only",
            "is_merged_pr_closeout": False,
            "requires_human_review": True,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            db_written=False,
            task_status_changed=False,
        ),
        next_allowed_actions=["resolve the missing task record and retry"],
        actions_not_performed=_actions_not_performed(include_state_changes=True),
        warnings=[error],
        blocking_warnings=[error],
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        error=error,
    )


def _actions_not_performed(*, include_state_changes: bool = False) -> list[str]:
    actions = [
        "GitHub mutation",
        "GitHub issue close",
        "PR merge",
        "PR creation",
        "branch push",
        "local branch deletion",
        "remote branch deletion",
        "worktree removal",
        "executor run",
        "validator run",
        "cron/systemd/deploy modification",
        "automation or scheduler loop start",
    ]
    if include_state_changes:
        actions.extend(
            [
                "task status update",
                "archive evidence recording",
            ]
        )
    return actions


def _safety_block(
    *,
    human_confirmation_confirmed: bool,
    db_written: bool,
    task_status_changed: bool,
) -> dict[str, Any]:
    return {
        "human_confirmation_required": True,
        "human_confirmation_confirmed": human_confirmation_confirmed,
        "db_written": db_written,
        "task_status_changed": task_status_changed,
        "github_mutated": False,
        "issue_closed": False,
        "branch_deleted": False,
        "worktree_deleted": False,
        "cleanup_performed": False,
        "executor_started": False,
        "validator_started": False,
        "cron_modified": False,
        "merge_performed": False,
        "pr_created": False,
        "automation_added": False,
        "scheduler_loop_started": False,
        "background_worker_started": False,
        "webhook_started": False,
        "polling_loop_started": False,
    }


def _normalize_reason_code(reason_code: str) -> str:
    normalized = (reason_code or "").strip()
    if normalized not in REASON_CODES:
        raise ValueError(
            "reason_code must be one of: " + ", ".join(REASON_CODES)
        )
    return normalized


def _normalize_target_status(status: str) -> str:
    normalized = validate_task_status(status)
    if normalized not in ARCHIVE_TARGET_STATUSES:
        raise ValueError(
            "target_status must be one of: "
            + ", ".join(sorted(ARCHIVE_TARGET_STATUSES))
        )
    return normalized


def _resolve_path(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator-confirmed evidence-only / superseded task archive. This is "
            "NOT merged-PR closeout and does not replace confirm_task_closeout.py."
        )
    )
    parser.add_argument(
        "--task-key", required=True, help="Task key, for example GH-9604."
    )
    parser.add_argument(
        "--reason-code",
        required=True,
        choices=list(REASON_CODES),
        help="Why the task is being archived as evidence-only / superseded.",
    )
    parser.add_argument(
        "--db-path",
        help="Path to the SQLite state DB. Default: ~/.agent-taskflow/state.db.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Optional artifact root used for the archive evidence file.",
    )
    parser.add_argument(
        "--note",
        help="Optional free-text operator note recorded in the archive evidence.",
    )
    parser.add_argument(
        "--superseded-by-pr",
        help="Optional PR number/reference that salvaged or superseded the task.",
    )
    parser.add_argument(
        "--superseded-by-task",
        help="Optional task key that superseded the task.",
    )
    parser.add_argument(
        "--target-status",
        default=DEFAULT_TARGET_STATUS,
        help="Terminal status to write on confirmation. Default: archived.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only. Never update the DB or write evidence.",
    )
    parser.add_argument(
        "--confirm-evidence-archive",
        action="store_true",
        help="Required before the archive performs any DB write.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit compact JSON. JSON is always the output format.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def _emit_json(payload: dict[str, Any], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _error_payload(task_key: str, reason_code: str | None, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "task_key": task_key,
        "reason_code": reason_code,
        "summary": message,
        "error": message,
        "safety": _safety_block(
            human_confirmation_confirmed=False,
            db_written=False,
            task_status_changed=False,
        ),
    }


def main(argv: list[str] | None = None, *, store: TaskMirrorStore | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = EvidenceArchiveRequest(
            task_key=args.task_key,
            reason_code=args.reason_code,
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            note=args.note,
            superseded_by_pr=args.superseded_by_pr,
            superseded_by_task=args.superseded_by_task,
            target_status=args.target_status,
            dry_run=args.dry_run,
            confirm_evidence_archive=args.confirm_evidence_archive,
        )
        result = archive_task_evidence_only(request, store=store)
    except (ValueError, OSError, EvidenceArchiveError) as exc:
        _emit_json(
            _error_payload(args.task_key, args.reason_code, str(exc)),
            compact=args.json and not args.pretty,
        )
        return 1

    _emit_json(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
