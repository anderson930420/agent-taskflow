"""Read-only waiting-approval review summary for Agent Taskflow.

This module assembles deterministic human-review evidence for tasks that have
already reached ``waiting_approval``. It only reads the local mirror database
and artifact files. It does not write the database, prepare worktrees, run
executors, run validators, push branches, create pull requests, approve,
merge, or clean up workspaces.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from agent_taskflow._helpers import dedupe_preserve_order as _dedupe_preserve_order
from agent_taskflow.api.review import (
    build_artifact_file_summaries,
    build_review_evidence,
    build_task_evidence_readback,
)
from agent_taskflow.api.schemas import json_safe
from agent_taskflow.models import (
    TaskArtifactRecord,
    TaskEventRecord,
    TaskRecord,
    TaskWorktreeRecord,
)
from agent_taskflow.tasks import normalize_task_key


APPROVED_TASK_STATUS = "waiting_approval"
DEFAULT_DB_PATH = Path.home() / ".agent-taskflow" / "state.db"
ISSUE_SPEC_FILENAME = "issue_spec.md"

_TASKS_QUERY = "SELECT * FROM tasks WHERE task_key = ?"
_ARTIFACTS_QUERY = "SELECT * FROM task_artifacts WHERE task_key = ? ORDER BY id ASC"
_WORKTREE_QUERY = "SELECT * FROM task_worktrees WHERE task_key = ?"
_EVENTS_QUERY = "SELECT * FROM task_events WHERE task_key = ? ORDER BY id ASC"

_NEXT_ALLOWED_ACTIONS = [
    "manual review",
    "human approval or rejection in a later phase",
    "generate PR handoff summary in a later phase",
    "run explicit branch push dry-run in a later phase",
    "run explicit draft PR dry-run in a later phase",
]

_ACTIONS_NOT_PERFORMED = [
    "branch push",
    "PR creation",
    "merge",
    "approval",
    "cleanup",
    "branch deletion",
    "worktree deletion",
]


class WaitingApprovalSummaryError(RuntimeError):
    """Raised when a waiting-approval summary cannot be generated."""


@dataclass(frozen=True)
class WaitingApprovalSummaryRequest:
    """Input for a read-only waiting-approval summary."""

    task_key: str
    db_path: Path | None = None
    artifact_root: Path | None = None
    allow_non_waiting: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        if self.db_path is not None:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser().resolve())
        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                Path(self.artifact_root).expanduser().resolve(),
            )


@dataclass(frozen=True)
class WaitingApprovalSummaryResult:
    """Structured read-only summary for human review."""

    ok: bool
    status: str
    task_key: str
    task: dict[str, Any]
    source: dict[str, Any]
    workspace: dict[str, Any]
    executor: dict[str, Any]
    validators: dict[str, Any]
    artifacts: list[dict[str, Any]]
    approval_review: dict[str, Any]
    evidence: dict[str, Any]
    review_readiness: dict[str, Any]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    summary: dict[str, Any]
    safety: dict[str, Any]
    warnings: list[str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


def summarize_waiting_approval_task(
    request: WaitingApprovalSummaryRequest,
) -> WaitingApprovalSummaryResult:
    """Build a deterministic summary for one mirrored task."""

    db_path = request.db_path or DEFAULT_DB_PATH
    if not db_path.exists():
        return _error_result(
            request.task_key,
            status="not_found",
            error=f"SQLite state DB not found: {db_path}",
        )

    try:
        with _connect_read_only(db_path) as conn:
            task = _load_task(conn, request.task_key)
            if task is None:
                return _error_result(
                    request.task_key,
                    status="not_found",
                    error=f"Task not found: {request.task_key}",
                )

            worktree = _load_worktree(conn, request.task_key)
            artifacts = _load_artifacts(conn, request.task_key)
            events = _load_events(conn, request.task_key)
    except sqlite3.Error as exc:
        return _error_result(
            request.task_key,
            status="error",
            error=f"Could not read local mirror DB: {exc}",
        )

    warnings: list[str] = []
    ready_for_human_review = True

    if task.status != APPROVED_TASK_STATUS:
        message = f"Task status is {task.status}; expected {APPROVED_TASK_STATUS}"
        warnings.append(message)
        ready_for_human_review = False
        if not request.allow_non_waiting:
            warnings.append(
                "Default mode requires waiting_approval; rerun with --allow-non-waiting to inspect other states."
            )

    effective_artifact_dir = _resolve_artifact_dir(task, request, artifacts)
    if effective_artifact_dir is None:
        warnings.append("Task has no artifact directory and no artifact-root fallback was provided")
        ready_for_human_review = False

    source = _build_source_summary(
        task=task,
        artifacts=artifacts,
        artifact_dir=effective_artifact_dir,
        warnings=warnings,
    )
    workspace = _build_workspace_summary(worktree=worktree, warnings=warnings)
    executor = _build_executor_summary(events, warnings=warnings)
    validators = _build_validator_summary(events, warnings=warnings)
    approval_review = _build_approval_review_summary(events)

    combined_artifacts = _build_artifact_summary(
        artifacts=artifacts,
        artifact_dir=effective_artifact_dir,
    )
    evidence = _build_evidence_summary(
        task_key=task.task_key,
        artifact_dir=effective_artifact_dir,
        task_artifacts=artifacts,
        validation_results=validators["results"],
    )

    if not source["available"]:
        ready_for_human_review = False
    if not workspace["available"]:
        ready_for_human_review = False
    if not executor["available"] or not executor["finished_ok"]:
        ready_for_human_review = False
    if not validators["available"] or not validators["all_passed"]:
        ready_for_human_review = False

    blocking_warnings = _blocking_warnings(
        task=task,
        source=source,
        workspace=workspace,
        executor=executor,
        validators=validators,
    )
    review_readiness = {
        "ready_for_human_review": ready_for_human_review,
        "blocking_warnings": blocking_warnings,
        "non_blocking_warnings": [
            warning for warning in warnings if warning not in blocking_warnings
        ],
        "recommended_human_checks": [
            "Review the issue/spec artifact and confirm it matches the task goal",
            "Inspect the prepared worktree and changed files",
            "Review executor output and validator results",
            "Confirm any approval/review evidence matches the intended human decision",
        ],
    }

    summary = {
        "ready_for_review": ready_for_human_review,
        "requires_human_decision": True,
        "next_phase": "manual_review",
        "task_status": task.status,
    }

    safety = _safety_block(
        task_status_changed=False,
        workspace_prepared=False,
        executor_started=False,
        validators_started=False,
        artifact_written=False,
        db_written=False,
    )

    ok = request.allow_non_waiting or task.status == APPROVED_TASK_STATUS
    status = "ok" if ok else "blocked"
    if not request.allow_non_waiting and task.status != APPROVED_TASK_STATUS:
        ok = False
        status = "blocked"

    if source["available"] is False:
        warnings.append("Issue/spec source evidence is missing or unreadable")
    if workspace["available"] is False:
        warnings.append("Worktree evidence is missing or incomplete")
    if executor["available"] is False:
        warnings.append("Executor evidence is missing")
    if executor["available"] and not executor["finished_ok"]:
        warnings.append("Executor did not finish successfully")
    if validators["available"] is False:
        warnings.append("Validator evidence is missing")
    if validators["available"] and not validators["all_passed"]:
        warnings.append("At least one validator failed or was blocked")
    if approval_review["available"] is False:
        warnings.append("No approval/review evidence is present yet")

    return WaitingApprovalSummaryResult(
        ok=ok,
        status=status,
        task_key=task.task_key,
        task=_task_summary(task),
        source=source,
        workspace=workspace,
        executor=executor,
        validators=validators,
        artifacts=combined_artifacts,
        approval_review=approval_review,
        evidence=evidence,
        review_readiness=review_readiness,
        next_allowed_actions=list(_NEXT_ALLOWED_ACTIONS),
        actions_not_performed=list(_ACTIONS_NOT_PERFORMED),
        summary=summary,
        safety=safety,
        warnings=_dedupe_preserve_order(warnings),
        error=None,
    )


def summarize_waiting_approval_task_markdown(
    request: WaitingApprovalSummaryRequest,
) -> tuple[WaitingApprovalSummaryResult, str]:
    """Return the structured result and a human-readable markdown summary."""

    result = summarize_waiting_approval_task(request)
    return result, _format_markdown(result)


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_task(conn: sqlite3.Connection, task_key: str) -> TaskRecord | None:
    row = conn.execute(_TASKS_QUERY, (task_key,)).fetchone()
    if row is None:
        return None
    return TaskRecord(
        task_key=row["task_key"],
        project=row["project"],
        board=row["board"],
        hermes_task_id=row["hermes_task_id"],
        title=row["title"],
        status=row["status"],
        repo_path=Path(row["repo_path"]),
        artifact_dir=Path(row["artifact_dir"]) if row["artifact_dir"] else None,
        blocked_reason=row["blocked_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_synced_at=row["last_synced_at"],
        executor=row["executor"] if "executor" in row.keys() else None,
        model=row["model"] if "model" in row.keys() else None,
        provider=row["provider"] if "provider" in row.keys() else None,
        tools=_parse_task_tools(row["tools"]),
        pi_bin=row["pi_bin"] if "pi_bin" in row.keys() else None,
    )


def _load_artifacts(conn: sqlite3.Connection, task_key: str) -> list[TaskArtifactRecord]:
    rows = conn.execute(_ARTIFACTS_QUERY, (task_key,)).fetchall()
    artifacts: list[TaskArtifactRecord] = []
    for row in rows:
        artifacts.append(
            TaskArtifactRecord(
                task_key=row["task_key"],
                artifact_type=row["artifact_type"],
                path=Path(row["path"]),
                created_at=row["created_at"],
            )
        )
    return artifacts


def _load_worktree(conn: sqlite3.Connection, task_key: str) -> TaskWorktreeRecord | None:
    row = conn.execute(_WORKTREE_QUERY, (task_key,)).fetchone()
    if row is None:
        return None
    return TaskWorktreeRecord(
        task_key=row["task_key"],
        repo_path=Path(row["repo_path"]),
        worktree_path=Path(row["worktree_path"]),
        branch=row["branch"],
        base_branch=row["base_branch"],
        base_sha=row["base_sha"] if "base_sha" in row.keys() else None,
        status=row["status"],
        created_at=row["created_at"],
        cleaned_at=row["cleaned_at"],
    )


def _load_events(conn: sqlite3.Connection, task_key: str) -> list[TaskEventRecord]:
    rows = conn.execute(_EVENTS_QUERY, (task_key,)).fetchall()
    events: list[TaskEventRecord] = []
    for row in rows:
        events.append(
            TaskEventRecord(
                task_key=row["task_key"],
                event_type=row["event_type"],
                source=row["source"],
                message=row["message"],
                payload_json=row["payload_json"],
                created_at=row["created_at"],
            )
        )
    return events


def _resolve_artifact_dir(
    task: TaskRecord,
    request: WaitingApprovalSummaryRequest,
    artifacts: list[TaskArtifactRecord],
) -> Path | None:
    if task.artifact_dir is not None and task.artifact_dir.exists():
        return task.artifact_dir
    if task.artifact_dir is not None and not task.artifact_dir.exists():
        return task.artifact_dir
    if request.artifact_root is not None:
        candidate = request.artifact_root / task.task_key
        if candidate.exists() or any(
            Path(record.path).parent == candidate for record in artifacts
        ):
            return candidate
    return None


def _build_source_summary(
    *,
    task: TaskRecord,
    artifacts: list[TaskArtifactRecord],
    artifact_dir: Path | None,
    warnings: list[str],
) -> dict[str, Any]:
    source_records = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type in {"issue_spec", "spec"}
    ]
    if artifact_dir is not None and artifact_dir.exists():
        issue_path = artifact_dir / ISSUE_SPEC_FILENAME
        if issue_path.exists():
            source_records = [TaskArtifactRecord(
                task_key=task.task_key,
                artifact_type="issue_spec",
                path=issue_path,
                created_at=None,
            )]
            parsed = _parse_issue_spec(issue_path.read_text(encoding="utf-8", errors="replace"))
            return {
                "kind": "github_issue",
                "artifact_type": "issue_spec",
                "available": True,
                "repo": parsed.get("repo"),
                "issue_number": parsed.get("issue_number"),
                "issue_url": parsed.get("issue_url"),
                "title": parsed.get("title"),
                "labels": parsed.get("labels", []),
                "author": parsed.get("author"),
                "issue_state": parsed.get("issue_state"),
                "created_at": parsed.get("created_at"),
                "updated_at": parsed.get("updated_at"),
                "ingested_at": parsed.get("ingested_at"),
                "task_key": parsed.get("task_key", task.task_key),
                "artifact_path": str(issue_path),
                "artifact_records": [
                    _artifact_record_to_dict(record) for record in source_records
                ],
            }

    if source_records:
        record = source_records[0]
        warnings.append("Issue/spec artifact record exists but the file is not readable")
        return {
            "kind": "github_issue",
            "artifact_type": record.artifact_type,
            "available": False,
            "repo": None,
            "issue_number": None,
            "issue_url": None,
            "title": None,
            "labels": [],
            "author": None,
            "issue_state": None,
            "created_at": None,
            "updated_at": None,
            "ingested_at": None,
            "task_key": task.task_key,
            "artifact_path": str(record.path),
            "artifact_records": [_artifact_record_to_dict(item) for item in source_records],
        }

    return {
        "kind": "github_issue",
        "artifact_type": "issue_spec",
        "available": False,
        "repo": None,
        "issue_number": None,
        "issue_url": None,
        "title": None,
        "labels": [],
        "author": None,
        "issue_state": None,
        "created_at": None,
        "updated_at": None,
        "ingested_at": None,
        "task_key": task.task_key,
        "artifact_path": None,
        "artifact_records": [],
    }


def _build_workspace_summary(
    *,
    worktree: TaskWorktreeRecord | None,
    warnings: list[str],
) -> dict[str, Any]:
    if worktree is None:
        warnings.append("Worktree record is missing")
        return {
            "available": False,
            "worktree_path": None,
            "path_exists": False,
            "branch": None,
            "base_branch": None,
            "base_sha": None,
            "status": None,
            "created_at": None,
            "cleaned_at": None,
        }

    path_exists = worktree.worktree_path.exists()
    if not path_exists:
        warnings.append(f"Worktree path is missing on disk: {worktree.worktree_path}")

    return {
        "available": path_exists,
        "worktree_path": str(worktree.worktree_path),
        "path_exists": path_exists,
        "branch": worktree.branch,
        "base_branch": worktree.base_branch,
        "base_sha": worktree.base_sha,
        "status": worktree.status,
        "created_at": worktree.created_at,
        "cleaned_at": worktree.cleaned_at,
        "repo_path": str(worktree.repo_path),
    }


def _build_executor_summary(
    events: list[TaskEventRecord],
    *,
    warnings: list[str],
) -> dict[str, Any]:
    runs = _executor_runs(events)
    available = bool(runs)
    current = runs[-1] if runs else None
    finished_ok = bool(current and current["status"] in {"completed", "passed"} and current["exit_code"] in {0, None})
    if not available:
        warnings.append("No executor run evidence was found")
    elif not finished_ok:
        warnings.append("Executor run evidence does not show a successful completion")

    return {
        "available": available,
        "executor": current["executor"] if current else None,
        "started_at": current["started_at"] if current else None,
        "finished_at": current["finished_at"] if current else None,
        "finished_ok": finished_ok,
        "summary": current["summary"] if current else None,
        "run_id": current["run_id"] if current else None,
        "runs": runs,
    }


def _build_validator_summary(
    events: list[TaskEventRecord],
    *,
    warnings: list[str],
) -> dict[str, Any]:
    results = _validation_results(events)
    available = bool(results)
    by_validator = {result["validator"]: result for result in results if result["validator"]}
    current_results = [by_validator[key] for key in sorted(by_validator)]
    all_passed = bool(current_results) and all(
        item["status"] in {"passed", "completed"} and item["exit_code"] in {0, None}
        for item in current_results
    )
    if not available:
        warnings.append("No validator evidence was found")
    elif not all_passed:
        warnings.append("At least one validator did not pass")

    return {
        "available": available,
        "all_passed": all_passed,
        "failed_validators": [
            item["validator"] for item in current_results if item["status"] not in {"passed", "completed"} or item["exit_code"] not in {0, None}
        ],
        "results": current_results,
    }


def _build_approval_review_summary(events: list[TaskEventRecord]) -> dict[str, Any]:
    decisions = _approval_decisions(events)
    latest = decisions[-1] if decisions else None
    return {
        "available": bool(decisions),
        "decisions": decisions,
        "latest_decision": latest,
        "latest_decision_kind": latest["decision"] if latest else None,
    }


def _build_artifact_summary(
    *,
    artifacts: list[TaskArtifactRecord],
    artifact_dir: Path | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for artifact in artifacts:
        item = _artifact_record_to_dict(artifact)
        items.append(item)
        seen_paths.add(item["path"])

    if artifact_dir is not None and artifact_dir.exists():
        for summary in build_artifact_file_summaries(artifact_dir):
            path = str(artifact_dir / summary["name"])
            if path in seen_paths:
                continue
            items.append(
                {
                    "kind": summary["kind"],
                    "artifact_type": summary["kind"],
                    "name": summary["name"],
                    "path": path,
                    "available": True,
                        "source": "filesystem",
                        "created_at": None,
                        "size_bytes": summary["size_bytes"],
                        "preview_available": summary["preview_available"],
                        "preview_reason": summary.get("preview_reason"),
                        "is_executor_log": summary["is_executor_log"],
                        "is_validator_log": summary["is_validator_log"],
                        "is_mission_contract": summary["is_mission_contract"],
                    }
                )

    items.sort(key=lambda item: (str(item.get("kind")), str(item.get("path")), str(item.get("source", ""))))
    return items


def _build_evidence_summary(
    *,
    task_key: str,
    artifact_dir: Path | None,
    task_artifacts: list[TaskArtifactRecord],
    validation_results: list[dict[str, Any]],
) -> dict[str, Any]:
    if artifact_dir is None or not artifact_dir.exists():
        return {
            "available": False,
            "task_evidence": {
                "available": False,
                "categories": {},
                "summary": {},
                "safety": {
                    "read_only": True,
                    "push_available_from_this_endpoint": False,
                    "pr_creation_available_from_this_endpoint": False,
                    "merge_available_from_this_endpoint": False,
                    "cleanup_available_from_this_endpoint": False,
                    "approval_available_from_this_endpoint": False,
                },
            },
            "review_evidence": {
                "available": False,
                "artifact_index": None,
                "summary": None,
                "review_artifacts": [],
            },
        }

    task_evidence = build_task_evidence_readback(
        task_key=task_key,
        artifact_dir=artifact_dir,
        task_artifacts=task_artifacts,
        validation_results=validation_results,
    )
    review_evidence = build_review_evidence(
        task_key=task_key,
        artifact_dir=artifact_dir,
        validation_results=validation_results,
    )
    return {
        "available": True,
        "task_evidence": task_evidence,
        "review_evidence": review_evidence,
    }


def _task_summary(task: TaskRecord) -> dict[str, Any]:
    return {
        "task_key": task.task_key,
        "status": task.status,
        "title": task.title,
        "project": task.project,
        "board": task.board,
        "repo_path": str(task.repo_path),
        "artifact_dir": str(task.artifact_dir) if task.artifact_dir else None,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "last_synced_at": task.last_synced_at,
        "executor": task.executor,
        "model": task.model,
        "provider": task.provider,
        "tools": task.tools,
        "pi_bin": task.pi_bin,
        "blocked_reason": task.blocked_reason,
    }


def _artifact_record_to_dict(record: TaskArtifactRecord) -> dict[str, Any]:
    return {
        "kind": record.artifact_type,
        "artifact_type": record.artifact_type,
        "name": record.path.name,
        "path": str(record.path),
        "available": record.path.exists(),
        "source": "record",
        "created_at": record.created_at,
        "size_bytes": record.path.stat().st_size if record.path.exists() else None,
    }


def _parse_issue_spec(text: str) -> dict[str, Any]:
    patterns = {
        "task_key": re.compile(r"^- Task key: (.+)$", re.MULTILINE),
        "repo": re.compile(r"^- Repository: (.+)$", re.MULTILINE),
        "issue_number": re.compile(r"^- Issue number: (.+)$", re.MULTILINE),
        "issue_url": re.compile(r"^- Issue URL: (.+)$", re.MULTILINE),
        "issue_state": re.compile(r"^- Issue state: (.+)$", re.MULTILINE),
        "title": re.compile(r"^- Title: (.+)$", re.MULTILINE),
        "labels": re.compile(r"^- Labels: (.+)$", re.MULTILINE),
        "author": re.compile(r"^- Author: (.+)$", re.MULTILINE),
        "created_at": re.compile(r"^- Created at: (.+)$", re.MULTILINE),
        "updated_at": re.compile(r"^- Updated at: (.+)$", re.MULTILINE),
        "ingested_at": re.compile(r"^- Ingested at: (.+)$", re.MULTILINE),
    }

    data: dict[str, Any] = {}
    for key, pattern in patterns.items():
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1).strip()
        if value in {"(none)", "(unknown)", "(empty)"}:
            data[key] = None if key != "labels" else []
            continue
        if key == "issue_number":
            try:
                data[key] = int(value)
            except ValueError:
                data[key] = None
            continue
        if key == "labels":
            data[key] = [item.strip() for item in value.split(",") if item.strip()]
            continue
        data[key] = value
    if "labels" not in data:
        data["labels"] = []
    return data


def _executor_runs(events: list[TaskEventRecord]) -> list[dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        payload = _event_payload(event)
        if payload.get("kind") not in {"executor_run_started", "executor_run_finished"}:
            continue
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if run_id not in runs:
            runs[run_id] = {
                "run_id": run_id,
                "task_key": event.task_key,
                "executor": payload.get("executor"),
                "model": None,
                "prompt_path": None,
                "status": None,
                "exit_code": None,
                "summary": None,
                "log_path": None,
                "artifacts": {},
                "started_at": None,
                "finished_at": None,
            }
            order.append(run_id)
        run = runs[run_id]
        if payload.get("kind") == "executor_run_started":
            run["executor"] = payload.get("executor")
            run["model"] = payload.get("model")
            run["prompt_path"] = payload.get("prompt_path")
            run["started_at"] = event.created_at
        else:
            run["executor"] = payload.get("executor")
            run["status"] = payload.get("status")
            run["exit_code"] = payload.get("exit_code")
            run["summary"] = payload.get("summary")
            run["log_path"] = payload.get("log_path")
            run["artifacts"] = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
            run["finished_at"] = event.created_at
    return [runs[run_id] for run_id in order]


def _validation_results(events: list[TaskEventRecord]) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        payload = _event_payload(event)
        if payload.get("kind") != "validation_result":
            continue
        validator = payload.get("validator")
        if not isinstance(validator, str) or not validator:
            continue
        if validator not in results:
            results[validator] = {
                "task_key": event.task_key,
                "validator": validator,
                "status": None,
                "exit_code": None,
                "summary": None,
                "log_path": None,
                "artifacts": {},
                "created_at": None,
            }
            order.append(validator)
        result = results[validator]
        result["status"] = payload.get("status")
        result["exit_code"] = payload.get("exit_code")
        result["summary"] = payload.get("summary")
        result["log_path"] = payload.get("log_path")
        result["artifacts"] = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
        result["created_at"] = event.created_at
    return [results[validator] for validator in order]


def _approval_decisions(events: list[TaskEventRecord]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for event in events:
        payload = _event_payload(event)
        if payload.get("kind") not in {"approval_decision", "approval_recorded"}:
            continue
        reviewer = payload.get("reviewer") or payload.get("decided_by")
        decisions.append(
            {
                "task_key": event.task_key,
                "decision": payload.get("decision"),
                "decided_by": payload.get("decided_by") or reviewer,
                "notes": payload.get("notes") or payload.get("summary"),
                "reviewer": reviewer,
                "summary": payload.get("summary") or payload.get("notes"),
                "reason": payload.get("reason"),
                "pr_url": payload.get("pr_url"),
                "pr_number": payload.get("pr_number"),
                "merged_commit": payload.get("merged_commit"),
                "created_at": event.created_at,
            }
        )
    return decisions


def _event_payload(event: TaskEventRecord) -> dict[str, Any]:
    if not event.payload_json:
        return {}
    try:
        payload = json.loads(event.payload_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _parse_task_tools(raw: Any) -> list[str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return list(parsed)
    return None


def _blocking_warnings(
    *,
    task: TaskRecord,
    source: dict[str, Any],
    workspace: dict[str, Any],
    executor: dict[str, Any],
    validators: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if task.status != APPROVED_TASK_STATUS:
        warnings.append(f"Task status is {task.status}; expected {APPROVED_TASK_STATUS}")
    if not source["available"]:
        warnings.append("Issue/spec evidence is missing")
    if not workspace["available"]:
        warnings.append("Worktree evidence is missing")
    if not executor["available"]:
        warnings.append("Executor evidence is missing")
    if executor["available"] and not executor["finished_ok"]:
        warnings.append("Executor did not finish successfully")
    if not validators["available"]:
        warnings.append("Validator evidence is missing")
    if validators["available"] and not validators["all_passed"]:
        warnings.append("At least one validator failed or was blocked")
    return warnings


def _safety_block(
    *,
    task_status_changed: bool,
    workspace_prepared: bool,
    executor_started: bool,
    validators_started: bool,
    artifact_written: bool,
    db_written: bool,
) -> dict[str, Any]:
    return {
        "read_only": True,
        "task_status_changed": task_status_changed,
        "db_written": db_written,
        "artifact_written": artifact_written,
        "workspace_prepared": workspace_prepared,
        "executor_started": executor_started,
        "validators_started": validators_started,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "branch_deleted": False,
        "worktree_deleted": False,
        "background_worker_started": False,
        "webhook_started": False,
        "polling_loop_started": False,
    }


def _error_result(task_key: str, *, status: str, error: str) -> WaitingApprovalSummaryResult:
    return WaitingApprovalSummaryResult(
        ok=False,
        status=status,
        task_key=task_key,
        task={
            "task_key": task_key,
            "status": None,
            "title": None,
            "project": None,
            "board": None,
            "repo_path": None,
            "artifact_dir": None,
            "created_at": None,
            "updated_at": None,
            "last_synced_at": None,
            "executor": None,
            "model": None,
            "provider": None,
            "tools": None,
            "pi_bin": None,
            "blocked_reason": None,
        },
        source={
            "kind": "github_issue",
            "artifact_type": "issue_spec",
            "available": False,
            "repo": None,
            "issue_number": None,
            "issue_url": None,
            "title": None,
            "labels": [],
            "author": None,
            "issue_state": None,
            "created_at": None,
            "updated_at": None,
            "ingested_at": None,
            "task_key": task_key,
            "artifact_path": None,
            "artifact_records": [],
        },
        workspace={
            "available": False,
            "worktree_path": None,
            "path_exists": False,
            "branch": None,
            "base_branch": None,
            "base_sha": None,
            "status": None,
            "created_at": None,
            "cleaned_at": None,
            "repo_path": None,
        },
        executor={
            "available": False,
            "executor": None,
            "started_at": None,
            "finished_at": None,
            "finished_ok": False,
            "summary": None,
            "run_id": None,
            "runs": [],
        },
        validators={
            "available": False,
            "all_passed": False,
            "failed_validators": [],
            "results": [],
        },
        artifacts=[],
        approval_review={
            "available": False,
            "decisions": [],
            "latest_decision": None,
            "latest_decision_kind": None,
        },
        evidence={
            "available": False,
            "task_evidence": {
                "available": False,
                "categories": {},
                "summary": {},
                "safety": _safety_block(
                    task_status_changed=False,
                    workspace_prepared=False,
                    executor_started=False,
                    validators_started=False,
                    artifact_written=False,
                    db_written=False,
                ),
            },
            "review_evidence": {
                "available": False,
                "artifact_index": None,
                "summary": None,
                "review_artifacts": [],
            },
        },
        review_readiness={
            "ready_for_human_review": False,
            "blocking_warnings": [error],
            "non_blocking_warnings": [],
            "recommended_human_checks": [],
        },
        next_allowed_actions=list(_NEXT_ALLOWED_ACTIONS),
        actions_not_performed=list(_ACTIONS_NOT_PERFORMED),
        summary={
            "ready_for_review": False,
            "requires_human_decision": True,
            "next_phase": "manual_review",
            "task_status": None,
        },
        safety=_safety_block(
            task_status_changed=False,
            workspace_prepared=False,
            executor_started=False,
            validators_started=False,
            artifact_written=False,
            db_written=False,
        ),
        warnings=[error],
        error=error,
    )


def _format_markdown(result: WaitingApprovalSummaryResult) -> str:
    lines = [
        "# Waiting Approval Review Summary",
        "",
        f"- Task key: {result.task_key}",
        f"- Summary status: {result.status}",
        f"- Task status: {result.task['status']}",
        f"- Ready for human review: {result.review_readiness['ready_for_human_review']}",
        "",
        "## Source",
        f"- Available: {result.source['available']}",
        f"- Issue URL: {result.source['issue_url'] or '(none)'}",
        f"- Title: {result.source['title'] or '(none)'}",
        f"- Labels: {', '.join(result.source['labels']) if result.source['labels'] else '(none)'}",
        "",
        "## Workspace",
        f"- Available: {result.workspace['available']}",
        f"- Worktree path: {result.workspace['worktree_path'] or '(none)'}",
        f"- Branch: {result.workspace['branch'] or '(none)'}",
        "",
        "## Executor",
        f"- Available: {result.executor['available']}",
        f"- Finished ok: {result.executor['finished_ok']}",
        f"- Summary: {result.executor['summary'] or '(none)'}",
        "",
        "## Validators",
        f"- Available: {result.validators['available']}",
        f"- All passed: {result.validators['all_passed']}",
        "",
        "## Review Readiness",
        f"- Ready for human review: {result.review_readiness['ready_for_human_review']}",
    ]
    if result.review_readiness["blocking_warnings"]:
        lines.extend(["- Blocking warnings:"])
        lines.extend(f"  - {warning}" for warning in result.review_readiness["blocking_warnings"])
    if result.warnings:
        lines.extend(["- Warnings:"])
        lines.extend(f"  - {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


__all__ = [
    "WaitingApprovalSummaryError",
    "WaitingApprovalSummaryRequest",
    "WaitingApprovalSummaryResult",
    "summarize_waiting_approval_task",
    "summarize_waiting_approval_task_markdown",
]
