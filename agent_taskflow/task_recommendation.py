"""Read-only queued task recommendation for Agent Taskflow."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_taskflow.store import default_db_path


BLOCKED_LABELS = frozenset({"blocked", "do-not-run", "no-agent"})
HIGH_RISK_LABELS = frozenset({"high-risk", "needs-human"})
LOW_RISK_LABELS = frozenset({"low-risk", "docs", "documentation", "test", "tests"})
READY_LABELS = frozenset({"ready"})

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

SAFETY_BLOCK = {
    "read_only": True,
    "task_status_changed": False,
    "db_written": False,
    "artifact_written": False,
    "workspace_prepared": False,
    "executor_started": False,
    "validators_started": False,
    "branch_pushed": False,
    "pr_created": False,
    "merged": False,
    "approved": False,
    "cleanup_performed": False,
}


class TaskRecommendationError(RuntimeError):
    """Raised when queued-task recommendation cannot continue."""


@dataclass(frozen=True)
class TaskRecommendationRequest:
    """Request for a one-shot queued task recommendation."""

    db_path: Path | None = None
    project: str | None = None
    limit: int = 10
    include_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    max_risk: str = "high"

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit must be positive")

        project = self.project.strip() if self.project is not None else None
        if project == "":
            raise ValueError("project must not be empty")
        object.__setattr__(self, "project", project)

        normalized_max_risk = self.max_risk.strip().lower()
        if normalized_max_risk not in RISK_ORDER:
            raise ValueError("max_risk must be one of: low, medium, high")
        object.__setattr__(self, "max_risk", normalized_max_risk)

        object.__setattr__(self, "include_labels", _normalize_labels(self.include_labels))
        object.__setattr__(self, "exclude_labels", _normalize_labels(self.exclude_labels))

        if self.db_path is None:
            object.__setattr__(self, "db_path", default_db_path())
        else:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser())


@dataclass(frozen=True)
class _TaskContext:
    task_key: str
    project: str
    status: str
    title: str | None
    artifact_dir: str | None
    created_at: str
    updated_at: str
    labels: tuple[str, ...]
    issue_number: int | None
    issue_url: str | None
    issue_spec_path: str | None
    has_issue_spec: bool


def recommend_next_tasks(
    request: TaskRecommendationRequest,
) -> dict[str, Any]:
    """Rank queued tasks without mutating the local task mirror."""

    queued_tasks = _read_queued_task_contexts(request.db_path, project=request.project)

    ranked: list[dict[str, Any]] = []
    blocked_or_excluded: list[dict[str, Any]] = []

    for task in queued_tasks:
        evaluation = _evaluate_task(
            task,
            include_labels=request.include_labels,
            exclude_labels=request.exclude_labels,
            max_risk=request.max_risk,
        )
        if evaluation["recommendable"]:
            ranked.append(evaluation)
        else:
            blocked_or_excluded.append(evaluation)

    ranked.sort(
        key=lambda item: (
            RISK_ORDER[item["risk_level"]],
            -int(item["score"]),
            item["created_at"] or "\uffff",
            item["updated_at"] or "\uffff",
            item["task_key"],
        )
    )
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index

    recommended_next_task = None
    if ranked:
        recommended_next_task = dict(ranked[0])
        recommended_next_task["requires_human_confirmation"] = True

    summary = {
        "queued_task_count": len(queued_tasks),
        "recommended_count": len(ranked),
        "blocked_or_excluded_count": len(blocked_or_excluded),
    }

    return {
        "ok": True,
        "status": "ok",
        "db_path": str(Path(request.db_path).expanduser()),
        "project": request.project,
        "limit": request.limit,
        "include_labels": list(request.include_labels),
        "exclude_labels": list(request.exclude_labels),
        "max_risk": request.max_risk,
        "recommended_next_task": recommended_next_task,
        "ranked_tasks": ranked[: request.limit],
        "blocked_or_excluded": blocked_or_excluded,
        "summary": summary,
        "safety": dict(SAFETY_BLOCK),
    }


def read_task_recommendations(
    db_path: str | Path | None = None,
    *,
    project: str | None = None,
    limit: int = 10,
    include_labels: tuple[str, ...] = (),
    exclude_labels: tuple[str, ...] = (),
    max_risk: str = "high",
) -> dict[str, Any]:
    """Convenience wrapper around :func:`recommend_next_tasks`."""

    request = TaskRecommendationRequest(
        db_path=Path(db_path).expanduser() if db_path is not None else None,
        project=project,
        limit=limit,
        include_labels=include_labels,
        exclude_labels=exclude_labels,
        max_risk=max_risk,
    )
    return recommend_next_tasks(request)


def _read_queued_task_contexts(
    db_path: Path,
    *,
    project: str | None,
) -> list[_TaskContext]:
    path = Path(db_path).expanduser()
    if not path.exists():
        return []

    try:
        conn = sqlite3.connect(_sqlite_read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise TaskRecommendationError(f"could not open DB read-only: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn, "tasks"):
            return []

        params: list[str] = ["queued"]
        clauses = ["status = ?"]
        if project is not None:
            clauses.append("project = ?")
            params.append(project)

        task_rows = conn.execute(
            f"""
            SELECT *
            FROM tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, task_key ASC
            """,
            params,
        ).fetchall()
        if not task_rows:
            return []

        task_keys = [str(row["task_key"]) for row in task_rows]
        labels_by_task = _load_labels_by_task(conn, task_keys)
        issue_spec_paths = _load_issue_spec_paths(conn, task_keys)

        contexts: list[_TaskContext] = []
        for row in task_rows:
            task_key = str(row["task_key"])
            contexts.append(
                _TaskContext(
                    task_key=task_key,
                    project=str(row["project"]),
                    status=str(row["status"]),
                    title=row["title"],
                    artifact_dir=row["artifact_dir"],
                    created_at=str(row["created_at"] or ""),
                    updated_at=str(row["updated_at"] or ""),
                    labels=labels_by_task.get(task_key, ()),
                    issue_number=_issue_number_from_task_key(task_key),
                    issue_url=_issue_url_from_events(conn, task_key),
                    issue_spec_path=issue_spec_paths.get(task_key),
                    has_issue_spec=task_key in issue_spec_paths,
                )
            )

        return contexts
    except sqlite3.Error as exc:
        raise TaskRecommendationError(f"could not read DB: {exc}") from exc
    finally:
        conn.close()


def _evaluate_task(
    task: _TaskContext,
    *,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
    max_risk: str,
) -> dict[str, Any]:
    labels = {_normalize_label(label) for label in task.labels}
    include_missing = [label for label in include_labels if label not in labels]
    exclude_present = [label for label in exclude_labels if label in labels]

    if include_missing:
        return {
            "recommendable": False,
            "task_key": task.task_key,
            "project": task.project,
            "status": task.status,
            "title": task.title,
            "labels": list(task.labels),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "risk_level": "medium",
            "reason": "missing required include label(s)",
            "missing_labels": include_missing,
            "score": 0,
        }

    if exclude_present:
        return {
            "recommendable": False,
            "task_key": task.task_key,
            "project": task.project,
            "status": task.status,
            "title": task.title,
            "labels": list(task.labels),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "risk_level": "medium",
            "reason": "has excluded label(s)",
            "excluded_labels": exclude_present,
            "score": 0,
        }

    blocked_labels = sorted(labels & BLOCKED_LABELS)
    if blocked_labels:
        return {
            "recommendable": False,
            "task_key": task.task_key,
            "project": task.project,
            "status": task.status,
            "title": task.title,
            "labels": list(task.labels),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "risk_level": "high",
            "reason": "blocked label present",
            "blocked_labels": blocked_labels,
            "score": 0,
        }

    if not task.title or not str(task.title).strip():
        return {
            "recommendable": False,
            "task_key": task.task_key,
            "project": task.project,
            "status": task.status,
            "title": task.title,
            "labels": list(task.labels),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "risk_level": "high",
            "reason": "task title is missing or empty",
            "score": 0,
        }

    risk_level, score, reason_parts = _score_task(task, labels)
    if RISK_ORDER[risk_level] > RISK_ORDER[max_risk]:
        return {
            "recommendable": False,
            "task_key": task.task_key,
            "project": task.project,
            "status": task.status,
            "title": task.title,
            "labels": list(task.labels),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "risk_level": risk_level,
            "reason": "task exceeds max risk threshold",
            "score": score,
        }

    reason_parts.append("requires human confirmation")
    return {
        "recommendable": True,
        "task_key": task.task_key,
        "project": task.project,
        "status": task.status,
        "title": task.title,
        "labels": list(task.labels),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "issue_number": task.issue_number,
        "issue_url": task.issue_url,
        "artifact_dir": task.artifact_dir,
        "issue_spec_path": task.issue_spec_path,
        "risk_level": risk_level,
        "reason": ", ".join(reason_parts),
        "score": score,
        "requires_human_confirmation": True,
    }


def _score_task(task: _TaskContext, labels: set[str]) -> tuple[str, int, list[str]]:
    score = 0
    reasons: list[str] = []

    if labels & READY_LABELS:
        score += 100
        reasons.append("ready label")

    if labels & {"low-risk"}:
        score += 30
        reasons.append("low-risk label")

    docs_labels = labels & {"docs", "documentation", "test", "tests"}
    if docs_labels:
        score += 15
        reasons.append("docs/test label")

    if task.has_issue_spec:
        score += 10
        reasons.append("issue_spec artifact present")
    else:
        score -= 15
        reasons.append("missing issue_spec artifact")

    if labels & HIGH_RISK_LABELS:
        score -= 25
        reasons.append("high-risk or needs-human label")
        risk_level = "high"
    elif labels & (READY_LABELS | LOW_RISK_LABELS):
        risk_level = "low"
    elif task.has_issue_spec:
        risk_level = "medium"
    else:
        risk_level = "high"

    if task.title and re.search(r"\bhigh[- ]risk\b", str(task.title), re.IGNORECASE):
        score -= 15
        reasons.append("title mentions high risk")
        risk_level = "high"

    if task.title and re.search(r"\bready\b", str(task.title), re.IGNORECASE):
        score += 10
        reasons.append("title mentions ready")

    if not reasons:
        reasons.append("queued task with no blocked labels")

    return risk_level, score, reasons


def _load_labels_by_task(
    conn: sqlite3.Connection,
    task_keys: list[str],
) -> dict[str, tuple[str, ...]]:
    if not task_keys:
        return {}

    placeholders = ", ".join("?" for _ in task_keys)
    rows = conn.execute(
        f"""
        SELECT task_key, payload_json
        FROM task_events
        WHERE event_type = 'github_issue_ingested'
          AND task_key IN ({placeholders})
        ORDER BY id ASC
        """,
        task_keys,
    ).fetchall()

    labels_by_task: dict[str, tuple[str, ...]] = {}
    for row in rows:
        payload = _json_object(row["payload_json"])
        labels = payload.get("labels")
        if not isinstance(labels, list):
            continue
        normalized = tuple(
            label
            for label in (_normalize_label(str(label)) for label in labels)
            if label
        )
        if normalized:
            labels_by_task[str(row["task_key"])] = normalized
    return labels_by_task


def _load_issue_spec_paths(
    conn: sqlite3.Connection,
    task_keys: list[str],
) -> dict[str, str]:
    if not task_keys:
        return {}

    placeholders = ", ".join("?" for _ in task_keys)
    rows = conn.execute(
        f"""
        SELECT task_key, path
        FROM task_artifacts
        WHERE artifact_type = 'issue_spec'
          AND task_key IN ({placeholders})
        ORDER BY id ASC
        """,
        task_keys,
    ).fetchall()

    result: dict[str, str] = {}
    for row in rows:
        result[str(row["task_key"])] = str(row["path"])
    return result


def _issue_url_from_events(conn: sqlite3.Connection, task_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT payload_json
        FROM task_events
        WHERE task_key = ?
          AND event_type = 'github_issue_ingested'
        ORDER BY id ASC
        LIMIT 1
        """,
        (task_key,),
    ).fetchone()
    if row is None:
        return None
    payload = _json_object(row["payload_json"])
    issue_url = payload.get("issue_url")
    return str(issue_url) if issue_url else None


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


def _issue_number_from_task_key(task_key: str) -> int | None:
    match = re.fullmatch(r"AT-GH-(\d+)", task_key)
    if not match:
        return None
    return int(match.group(1))


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for label in labels:
        value = _normalize_label(label)
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _normalize_label(label: str) -> str:
    return str(label or "").strip().lower()
