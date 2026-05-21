"""Read-only per-task operator recommendations.

This module classifies mirrored task evidence and recommends the next safe
human-driven phase. It never runs workflow actions, mutates the local mirror,
starts executors or validators, pushes branches, creates PRs, merges, approves,
or performs cleanup.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_taskflow.models import validate_task_status
from agent_taskflow.store import default_db_path
from agent_taskflow.tasks import normalize_task_key


COMPLETED_STATUSES = frozenset({"completed", "done"})
PR_HANDOFF_ARTIFACT_TYPES = frozenset({"pr_handoff_package", "pr_handoff"})
PR_HANDOFF_EVENT_TYPES = frozenset(
    {"pr_handoff_package_created", "pr_handoff_created"}
)

RECOMMENDED_COMMAND_KINDS = frozenset(
    {
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
        "no_action",
        "unknown",
    }
)

SAFETY_FLAGS: dict[str, bool] = {
    "read_only": True,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_cleanup": False,
    "will_approve": False,
    "will_reject": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_db": False,
    "will_mutate_github": False,
}


class TaskRecommendationsError(RuntimeError):
    """Raised when recommendations cannot be read safely."""


@dataclass(frozen=True)
class TaskRecommendationsRequest:
    """Read-only recommendation list request."""

    db_path: Path | None = None
    status: str | None = None
    project: str | None = None
    task_key: str | None = None
    completed_limit: int = 10

    def __post_init__(self) -> None:
        if self.db_path is None:
            object.__setattr__(self, "db_path", default_db_path())
        else:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser())

        if self.status is not None:
            object.__setattr__(self, "status", validate_task_status(self.status))

        if self.project is not None:
            project = self.project.strip()
            if not project:
                raise ValueError("project must not be empty")
            object.__setattr__(self, "project", project)

        if self.task_key is not None:
            object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        if self.completed_limit < 0:
            raise ValueError("completed_limit must be zero or positive")


@dataclass(frozen=True)
class _TaskContext:
    task: dict[str, Any]
    events: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    worktree: dict[str, Any] | None


@dataclass(frozen=True)
class _Evidence:
    task_execution_package: bool
    executor_available: bool
    executor_finished_ok: bool
    validators_available: bool
    validators_all_passed: bool
    failed_validators: tuple[str, ...]
    pr_handoff_package: bool
    branch_push_completed: bool
    draft_pr_available: bool
    draft_pr_verified: bool
    pr_merged: bool
    local_cleanup_completed: bool
    remote_branch_cleanup_completed: bool
    task_closeout_completed: bool
    active_worktree: bool
    branch_push_payload: dict[str, Any]
    draft_pr_payload: dict[str, Any]
    local_cleanup_payload: dict[str, Any]
    remote_cleanup_payload: dict[str, Any]
    closeout_payload: dict[str, Any]

    @property
    def any_cleanup(self) -> bool:
        return (
            self.local_cleanup_completed
            or self.remote_branch_cleanup_completed
            or self.task_closeout_completed
        )

    @property
    def cleanup_complete(self) -> bool:
        return self.task_closeout_completed or (
            self.local_cleanup_completed and self.remote_branch_cleanup_completed
        )


def list_task_recommendations(
    request: TaskRecommendationsRequest,
) -> dict[str, Any]:
    """Return deterministic read-only recommendations for mirrored tasks."""

    db_path = Path(request.db_path).expanduser()
    contexts = _read_contexts(request)
    items = [_recommend_for_context(context) for context in contexts]
    command_counts = Counter(item["recommended_command_kind"] for item in items)

    return {
        "ok": True,
        "status": "ok",
        "db_path": str(db_path),
        "filters": {
            "status": request.status,
            "project": request.project,
            "task_key": request.task_key,
        },
        "items": items,
        "count": len(items),
        "summary": {
            "recommendation_counts": dict(sorted(command_counts.items())),
            "read_only": True,
        },
        "safety_flags": dict(SAFETY_FLAGS),
    }


def recommend_tasks(
    db_path: str | Path | None = None,
    *,
    status: str | None = None,
    project: str | None = None,
    task_key: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for callers that do not need a request object."""

    return list_task_recommendations(
        TaskRecommendationsRequest(
            db_path=Path(db_path).expanduser() if db_path is not None else None,
            status=status,
            project=project,
            task_key=task_key,
        )
    )


def _read_contexts(request: TaskRecommendationsRequest) -> list[_TaskContext]:
    db_path = Path(request.db_path).expanduser()
    if not db_path.exists():
        raise TaskRecommendationsError(f"SQLite state DB not found: {db_path}")

    try:
        conn = sqlite3.connect(_sqlite_read_only_uri(db_path), uri=True)
    except sqlite3.Error as exc:
        raise TaskRecommendationsError(f"could not open DB read-only: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        if not _has_table(conn, "tasks"):
            return []

        task_rows = _load_task_rows(conn, request)
        task_keys = [str(row["task_key"]) for row in task_rows]
        events_by_task = _load_events_by_task(conn, task_keys)
        artifacts_by_task = _load_artifacts_by_task(conn, task_keys)
        worktrees_by_task = _load_worktrees_by_task(conn, task_keys)

        return [
            _TaskContext(
                task=_row_dict(row),
                events=events_by_task.get(str(row["task_key"]), []),
                artifacts=artifacts_by_task.get(str(row["task_key"]), []),
                worktree=worktrees_by_task.get(str(row["task_key"])),
            )
            for row in task_rows
        ]
    except sqlite3.Error as exc:
        raise TaskRecommendationsError(f"could not read DB: {exc}") from exc
    finally:
        conn.close()


def _load_task_rows(
    conn: sqlite3.Connection,
    request: TaskRecommendationsRequest,
) -> list[sqlite3.Row]:
    params: list[Any] = []
    clauses: list[str] = []

    if request.task_key is not None:
        clauses.append("task_key = ?")
        params.append(request.task_key)

    if request.project is not None:
        clauses.append("project = ?")
        params.append(request.project)

    if request.status is not None:
        clauses.append("status = ?")
        params.append(request.status)

    if request.task_key is None and request.status is None:
        base_clauses = list(clauses)
        base_params = list(params)
        non_completed_where = _where(base_clauses + ["status NOT IN (?, ?)"])
        non_completed_params = base_params + sorted(COMPLETED_STATUSES)
        rows = conn.execute(
            f"""
            SELECT *
            FROM tasks
            {non_completed_where}
            ORDER BY updated_at DESC, task_key ASC
            """,
            non_completed_params,
        ).fetchall()

        if request.completed_limit:
            completed_where = _where(base_clauses + ["status IN (?, ?)"])
            completed_params = base_params + sorted(COMPLETED_STATUSES)
            rows.extend(
                conn.execute(
                    f"""
                    SELECT *
                    FROM tasks
                    {completed_where}
                    ORDER BY updated_at DESC, task_key ASC
                    LIMIT ?
                    """,
                    completed_params + [request.completed_limit],
                ).fetchall()
            )
        return rows

    where = _where(clauses)
    return conn.execute(
        f"""
        SELECT *
        FROM tasks
        {where}
        ORDER BY updated_at DESC, task_key ASC
        """,
        params,
    ).fetchall()


def _load_events_by_task(
    conn: sqlite3.Connection,
    task_keys: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not task_keys or not _has_table(conn, "task_events"):
        return {}

    placeholders = ", ".join("?" for _ in task_keys)
    rows = conn.execute(
        f"""
        SELECT *
        FROM task_events
        WHERE task_key IN ({placeholders})
        ORDER BY id ASC
        """,
        task_keys,
    ).fetchall()

    events: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = _row_dict(row)
        events.setdefault(str(item["task_key"]), []).append(item)
    return events


def _load_artifacts_by_task(
    conn: sqlite3.Connection,
    task_keys: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not task_keys or not _has_table(conn, "task_artifacts"):
        return {}

    placeholders = ", ".join("?" for _ in task_keys)
    rows = conn.execute(
        f"""
        SELECT *
        FROM task_artifacts
        WHERE task_key IN ({placeholders})
        ORDER BY id ASC
        """,
        task_keys,
    ).fetchall()

    artifacts: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = _row_dict(row)
        artifacts.setdefault(str(item["task_key"]), []).append(item)
    return artifacts


def _load_worktrees_by_task(
    conn: sqlite3.Connection,
    task_keys: list[str],
) -> dict[str, dict[str, Any]]:
    if not task_keys or not _has_table(conn, "task_worktrees"):
        return {}

    placeholders = ", ".join("?" for _ in task_keys)
    rows = conn.execute(
        f"""
        SELECT *
        FROM task_worktrees
        WHERE task_key IN ({placeholders})
        """,
        task_keys,
    ).fetchall()
    return {str(row["task_key"]): _row_dict(row) for row in rows}


def _recommend_for_context(context: _TaskContext) -> dict[str, Any]:
    task = context.task
    evidence = _build_evidence(context)
    missing_evidence = _missing_evidence(evidence)
    inconsistent = _inconsistency_reasons(task, evidence)

    if inconsistent:
        decision = _decision(
            phase="inconsistent_evidence",
            action="Inspect task evidence.",
            kind="inspect_evidence",
            confidence="high",
            severity="medium",
            reason="Evidence is out of the expected workflow sequence: "
            + "; ".join(inconsistent),
            human_confirmation=False,
        )
    elif task["status"] == "queued":
        decision = _queued_decision(evidence)
    elif task["status"] == "waiting_approval":
        decision = _waiting_approval_decision(evidence)
    elif task["status"] == "blocked":
        decision = _decision(
            phase="blocked",
            action="Review blocker and rerun/fix.",
            kind="inspect_blocker",
            confidence="high",
            severity="high",
            reason=task.get("blocked_reason")
            or "Task is blocked and requires operator inspection.",
            human_confirmation=False,
        )
    elif task["status"] in COMPLETED_STATUSES:
        decision = _completed_decision(evidence)
    else:
        decision = _decision(
            phase="unknown",
            action="Inspect task evidence.",
            kind="unknown",
            confidence="medium",
            severity="medium",
            reason=f"Task status {task['status']!r} is not handled by this recommendation contract.",
            human_confirmation=False,
        )

    return {
        "task_key": task["task_key"],
        "project": task["project"],
        "title": task.get("title"),
        "status": task["status"],
        "blocked_reason": task.get("blocked_reason"),
        "current_phase_label": decision["current_phase_label"],
        "recommended_next_action": decision["recommended_next_action"],
        "recommended_command_kind": decision["recommended_command_kind"],
        "confidence": decision["confidence"],
        "severity": decision["severity"],
        "reason": decision["reason"],
        "required_human_confirmation": decision["required_human_confirmation"],
        "safety_flags": dict(SAFETY_FLAGS),
        "evidence_summary": _evidence_summary(evidence),
        "missing_evidence": missing_evidence,
        "related_artifacts": _related_artifacts(context.artifacts),
        "worktree_status": _worktree_status(context.worktree),
        "branch_status": _branch_status(evidence, context.worktree),
        "pr_status": _pr_status(evidence),
        "cleanup_status": _cleanup_status(evidence),
    }


def _queued_decision(evidence: _Evidence) -> dict[str, Any]:
    if evidence.task_execution_package:
        return _decision(
            phase="queued_handoff_ready",
            action="Run queued-task handoff.",
            kind="queued_task_handoff",
            confidence="high",
            severity="medium",
            reason="Task is queued and a Task Execution Package is present.",
            human_confirmation=True,
        )
    return _decision(
        phase="queued_needs_package",
        action="Create Task Execution Package.",
        kind="create_task_execution_package",
        confidence="high",
        severity="medium",
        reason="Task is queued and no Task Execution Package evidence is present.",
        human_confirmation=True,
    )


def _waiting_approval_decision(evidence: _Evidence) -> dict[str, Any]:
    if not evidence.executor_finished_ok or not evidence.validators_all_passed:
        return _decision(
            phase="waiting_approval_evidence_incomplete",
            action="Inspect task evidence.",
            kind="inspect_evidence",
            confidence="high",
            severity="medium",
            reason="Task is waiting_approval but executor or validator success evidence is missing.",
            human_confirmation=False,
        )
    if not evidence.pr_handoff_package:
        return _decision(
            phase="ready_for_pr_handoff",
            action="Create PR handoff package.",
            kind="pr_handoff_package",
            confidence="high",
            severity="medium",
            reason="Executor and validators passed, but PR handoff package evidence is missing.",
            human_confirmation=True,
        )
    if not evidence.branch_push_completed:
        return _decision(
            phase="pr_handoff_ready",
            action="Run branch push dry-run / confirm branch push.",
            kind="branch_push_review",
            confidence="high",
            severity="medium",
            reason="PR handoff package exists, but branch push evidence is missing.",
            human_confirmation=True,
        )
    if not evidence.draft_pr_available or not evidence.draft_pr_verified:
        return _decision(
            phase="branch_pushed",
            action="Run draft PR dry-run / confirm draft PR.",
            kind="draft_pr_review",
            confidence="high",
            severity="medium",
            reason="Branch push evidence exists, but verified draft PR evidence is missing.",
            human_confirmation=True,
        )
    if evidence.pr_merged and not evidence.any_cleanup:
        return _decision(
            phase="pr_merged",
            action="Run post-merge cleanup recommendation.",
            kind="post_merge_cleanup_review",
            confidence="high",
            severity="medium",
            reason="Draft PR evidence indicates the PR is merged, but cleanup evidence is missing.",
            human_confirmation=True,
        )
    if evidence.pr_merged and evidence.any_cleanup and not evidence.cleanup_complete:
        return _decision(
            phase="cleanup_in_progress",
            action="Continue cleanup / verify leftover branches/worktrees.",
            kind="cleanup_continue",
            confidence="high",
            severity="medium",
            reason="Cleanup evidence is partially complete after merged PR evidence.",
            human_confirmation=True,
        )
    if evidence.pr_merged and evidence.cleanup_complete:
        return _decision(
            phase="cleanup_complete",
            action="Continue cleanup / verify leftover branches/worktrees.",
            kind="cleanup_continue",
            confidence="medium",
            severity="low",
            reason="Cleanup evidence is present; closeout may still need operator verification.",
            human_confirmation=True,
        )
    return _decision(
        phase="draft_pr_open",
        action="Human review / merge PR manually.",
        kind="human_pr_review",
        confidence="high",
        severity="medium",
        reason="Verified draft PR evidence exists and no merged PR evidence is present.",
        human_confirmation=True,
    )


def _completed_decision(evidence: _Evidence) -> dict[str, Any]:
    if evidence.cleanup_complete:
        return _decision(
            phase="closed_out",
            action="No action needed.",
            kind="no_action",
            confidence="high",
            severity="info",
            reason="Task is completed and cleanup evidence is present.",
            human_confirmation=False,
        )
    return _decision(
        phase="completed_cleanup_incomplete",
        action="Continue cleanup / verify leftover branches/worktrees.",
        kind="cleanup_continue",
        confidence="medium",
        severity="low",
        reason="Task is completed, but cleanup evidence is incomplete.",
        human_confirmation=True,
    )


def _decision(
    *,
    phase: str,
    action: str,
    kind: str,
    confidence: str,
    severity: str,
    reason: str,
    human_confirmation: bool,
) -> dict[str, Any]:
    if kind not in RECOMMENDED_COMMAND_KINDS:
        raise AssertionError(f"unknown recommendation kind: {kind}")
    return {
        "current_phase_label": phase,
        "recommended_next_action": action,
        "recommended_command_kind": kind,
        "confidence": confidence,
        "severity": severity,
        "reason": reason,
        "required_human_confirmation": human_confirmation,
    }


def _build_evidence(context: _TaskContext) -> _Evidence:
    artifacts = context.artifacts
    events = context.events
    worktree = context.worktree

    task_execution_package = _has_artifact(artifacts, "task_execution_package") or _has_event(
        events,
        "task_execution_package_created",
    )
    executor_available, executor_finished_ok = _executor_state(events)
    validators_available, validators_all_passed, failed_validators = _validator_state(events)
    pr_handoff_package = any(
        _has_artifact(artifacts, artifact_type)
        for artifact_type in PR_HANDOFF_ARTIFACT_TYPES
    ) or any(_has_event(events, event_type) for event_type in PR_HANDOFF_EVENT_TYPES)

    branch_push_payload = _latest_payload(
        context,
        event_type="branch_push_completed",
        artifact_type="branch_push",
    )
    branch_push_completed = bool(
        _has_event(events, "branch_push_completed")
        or _has_artifact(artifacts, "branch_push")
    ) and branch_push_payload.get("push_ok", True) is not False

    draft_pr_payload = _latest_payload(
        context,
        event_type="draft_pr_created",
        artifact_type="draft_pr",
    )
    draft_pr_available = bool(
        _has_event(events, "draft_pr_created") or _has_artifact(artifacts, "draft_pr")
    )
    verification = draft_pr_payload.get("verification")
    draft_pr_verified = draft_pr_available and (
        draft_pr_payload.get("verified") is True
        or draft_pr_payload.get("draft_pr_verified") is True
        or (
            isinstance(verification, dict)
            and (
                verification.get("verified") is True
                or verification.get("passed") is True
            )
        )
    )
    pr_merged = _payload_indicates_merged_pr(draft_pr_payload)

    local_cleanup_payload = _latest_payload(
        context,
        event_type="local_cleanup_completed",
        artifact_type="local_cleanup",
    )
    remote_cleanup_payload = _latest_payload(
        context,
        event_type="remote_branch_cleanup_completed",
        artifact_type="remote_branch_cleanup",
    )
    closeout_payload = _latest_payload(
        context,
        event_type="task_closeout_completed",
        artifact_type="task_closeout",
    )

    local_cleanup_completed = bool(
        _has_event(events, "local_cleanup_completed")
        or _has_artifact(artifacts, "local_cleanup")
    )
    remote_branch_cleanup_completed = bool(
        _has_event(events, "remote_branch_cleanup_completed")
        or _has_artifact(artifacts, "remote_branch_cleanup")
    )
    task_closeout_completed = bool(
        _has_event(events, "task_closeout_completed")
        or _has_artifact(artifacts, "task_closeout")
    )
    active_worktree = bool(worktree and worktree.get("status") == "active")

    return _Evidence(
        task_execution_package=task_execution_package,
        executor_available=executor_available,
        executor_finished_ok=executor_finished_ok,
        validators_available=validators_available,
        validators_all_passed=validators_all_passed,
        failed_validators=tuple(failed_validators),
        pr_handoff_package=pr_handoff_package,
        branch_push_completed=branch_push_completed,
        draft_pr_available=draft_pr_available,
        draft_pr_verified=draft_pr_verified,
        pr_merged=pr_merged,
        local_cleanup_completed=local_cleanup_completed,
        remote_branch_cleanup_completed=remote_branch_cleanup_completed,
        task_closeout_completed=task_closeout_completed,
        active_worktree=active_worktree,
        branch_push_payload=branch_push_payload,
        draft_pr_payload=draft_pr_payload,
        local_cleanup_payload=local_cleanup_payload,
        remote_cleanup_payload=remote_cleanup_payload,
        closeout_payload=closeout_payload,
    )


def _executor_state(events: list[dict[str, Any]]) -> tuple[bool, bool]:
    runs: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    for event in events:
        payload = _json_object(event.get("payload_json"))
        if payload.get("kind") not in {"executor_run_started", "executor_run_finished"}:
            continue
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if run_id not in runs:
            runs[run_id] = {"status": None, "exit_code": None}
            ordered.append(run_id)
        if payload.get("kind") == "executor_run_finished":
            runs[run_id]["status"] = payload.get("status")
            runs[run_id]["exit_code"] = payload.get("exit_code")

    if not ordered:
        return False, False
    latest = runs[ordered[-1]]
    return True, latest.get("status") in {"completed", "passed"} and latest.get("exit_code") in {0, None}


def _validator_state(events: list[dict[str, Any]]) -> tuple[bool, bool, list[str]]:
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = _json_object(event.get("payload_json"))
        if payload.get("kind") != "validation_result":
            continue
        validator = payload.get("validator")
        if not isinstance(validator, str) or not validator:
            continue
        results[validator] = {
            "status": payload.get("status"),
            "exit_code": payload.get("exit_code"),
        }

    if not results:
        return False, False, []

    failed = [
        validator
        for validator, result in sorted(results.items())
        if result["status"] not in {"passed", "completed"}
        or result["exit_code"] not in {0, None}
    ]
    return True, not failed, failed


def _payload_indicates_merged_pr(payload: dict[str, Any]) -> bool:
    state_values = {
        str(payload.get("current_state") or "").upper(),
        str(payload.get("pr_state") or "").upper(),
        str(payload.get("state") or "").upper(),
    }
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    return (
        "MERGED" in state_values
        or payload.get("recorded_post_merge") is True
        or payload.get("merged") is True
        or safety.get("recorded_post_merge") is True
    )


def _inconsistency_reasons(task: dict[str, Any], evidence: _Evidence) -> list[str]:
    reasons: list[str] = []
    later_than_queue = (
        evidence.executor_available
        or evidence.validators_available
        or evidence.pr_handoff_package
        or evidence.branch_push_completed
        or evidence.draft_pr_available
        or evidence.any_cleanup
    )
    if task["status"] == "queued" and later_than_queue:
        reasons.append("queued task has post-queue execution or handoff evidence")

    if task["status"] == "waiting_approval":
        if evidence.branch_push_completed and not evidence.pr_handoff_package:
            reasons.append("branch push evidence exists without PR handoff evidence")
        if evidence.draft_pr_available and not evidence.branch_push_completed:
            reasons.append("draft PR evidence exists without branch push evidence")
        if evidence.pr_merged and not evidence.draft_pr_available:
            reasons.append("merged PR evidence exists without draft PR evidence")
        if evidence.any_cleanup and not evidence.pr_merged:
            reasons.append("cleanup evidence exists before merged PR evidence")
    return reasons


def _missing_evidence(evidence: _Evidence) -> list[str]:
    missing: list[str] = []
    checks = [
        ("task_execution_package", evidence.task_execution_package),
        ("executor_finished_ok", evidence.executor_finished_ok),
        ("validators_all_passed", evidence.validators_all_passed),
        ("pr_handoff_package", evidence.pr_handoff_package),
        ("branch_push_completed", evidence.branch_push_completed),
        ("draft_pr_verified", evidence.draft_pr_available and evidence.draft_pr_verified),
        ("pr_merged", evidence.pr_merged),
        ("local_cleanup_completed", evidence.local_cleanup_completed),
        ("remote_branch_cleanup_completed", evidence.remote_branch_cleanup_completed),
        ("task_closeout_completed", evidence.task_closeout_completed),
    ]
    for name, present in checks:
        if not present:
            missing.append(name)
    return missing


def _evidence_summary(evidence: _Evidence) -> dict[str, Any]:
    return {
        "task_execution_package": evidence.task_execution_package,
        "executor_available": evidence.executor_available,
        "executor_finished_ok": evidence.executor_finished_ok,
        "validators_available": evidence.validators_available,
        "validators_all_passed": evidence.validators_all_passed,
        "failed_validators": list(evidence.failed_validators),
        "pr_handoff_package": evidence.pr_handoff_package,
        "branch_push_completed": evidence.branch_push_completed,
        "draft_pr_available": evidence.draft_pr_available,
        "draft_pr_verified": evidence.draft_pr_verified,
        "pr_merged": evidence.pr_merged,
        "local_cleanup_completed": evidence.local_cleanup_completed,
        "remote_branch_cleanup_completed": evidence.remote_branch_cleanup_completed,
        "task_closeout_completed": evidence.task_closeout_completed,
        "cleanup_complete": evidence.cleanup_complete,
        "active_worktree": evidence.active_worktree,
    }


def _related_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "artifact_type": artifact.get("artifact_type"),
            "path": artifact.get("path"),
            "created_at": artifact.get("created_at"),
        }
        for artifact in artifacts
    ]


def _worktree_status(worktree: dict[str, Any] | None) -> dict[str, Any]:
    if worktree is None:
        return {
            "available": False,
            "status": None,
            "worktree_path": None,
            "path_exists": False,
            "branch": None,
            "base_branch": None,
            "base_sha": None,
            "cleaned_at": None,
        }

    worktree_path = worktree.get("worktree_path")
    path_exists = Path(str(worktree_path)).exists() if worktree_path else False
    return {
        "available": True,
        "status": worktree.get("status"),
        "worktree_path": worktree_path,
        "path_exists": path_exists,
        "branch": worktree.get("branch"),
        "base_branch": worktree.get("base_branch"),
        "base_sha": worktree.get("base_sha"),
        "cleaned_at": worktree.get("cleaned_at"),
    }


def _branch_status(
    evidence: _Evidence,
    worktree: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = evidence.branch_push_payload
    return {
        "branch": payload.get("branch") or (worktree or {}).get("branch"),
        "base_branch": payload.get("base_branch") or (worktree or {}).get("base_branch"),
        "head_sha": payload.get("head_sha"),
        "branch_pushed": evidence.branch_push_completed,
        "push_ok": payload.get("push_ok"),
        "remote": payload.get("remote"),
        "event_type": "branch_push_completed" if evidence.branch_push_completed else None,
    }


def _pr_status(evidence: _Evidence) -> dict[str, Any]:
    payload = evidence.draft_pr_payload
    return {
        "available": evidence.draft_pr_available,
        "pr_created": bool(payload.get("pr_created") or payload.get("draft_pr_created")),
        "draft_pr": payload.get("draft") if "draft" in payload else payload.get("draft_pr"),
        "draft_pr_verified": evidence.draft_pr_verified,
        "pr_number": payload.get("pr_number"),
        "pr_url": payload.get("pr_url"),
        "state": payload.get("current_state") or payload.get("pr_state") or payload.get("state"),
        "merged": evidence.pr_merged,
        "recorded_post_merge": bool(payload.get("recorded_post_merge")),
    }


def _cleanup_status(evidence: _Evidence) -> dict[str, Any]:
    return {
        "local_cleanup_completed": evidence.local_cleanup_completed,
        "remote_branch_cleanup_completed": evidence.remote_branch_cleanup_completed,
        "task_closeout_completed": evidence.task_closeout_completed,
        "cleanup_complete": evidence.cleanup_complete,
        "partial_cleanup": evidence.any_cleanup and not evidence.cleanup_complete,
        "local_cleanup_scope": evidence.local_cleanup_payload.get("cleanup_scope"),
        "remote_cleanup_scope": evidence.remote_cleanup_payload.get("cleanup_scope"),
    }


def _latest_payload(
    context: _TaskContext,
    *,
    event_type: str,
    artifact_type: str,
) -> dict[str, Any]:
    event_payloads = [
        _json_object(event.get("payload_json"))
        for event in context.events
        if event.get("event_type") == event_type
    ]
    if event_payloads:
        return event_payloads[-1]

    artifacts = [
        artifact
        for artifact in context.artifacts
        if artifact.get("artifact_type") == artifact_type and artifact.get("path")
    ]
    if not artifacts:
        return {}

    path = Path(str(artifacts[-1]["path"]))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _has_event(events: list[dict[str, Any]], event_type: str) -> bool:
    return any(event.get("event_type") == event_type for event in events)


def _has_artifact(artifacts: list[dict[str, Any]], artifact_type: str) -> bool:
    return any(artifact.get("artifact_type") == artifact_type for artifact in artifacts)


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _where(clauses: list[str]) -> str:
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


def _sqlite_read_only_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/:')}?mode=ro"


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


__all__ = [
    "RECOMMENDED_COMMAND_KINDS",
    "SAFETY_FLAGS",
    "TaskRecommendationsError",
    "TaskRecommendationsRequest",
    "list_task_recommendations",
    "recommend_tasks",
]
