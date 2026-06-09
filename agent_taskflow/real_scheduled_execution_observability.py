"""Read-only observability for the real scheduled execution path.

This module summarizes the real, cron-driven GitHub Issue one-task scheduler
tick (Level 10H) for operators. It reads a JSONL scheduler tick log file and the
local task mirror database. It is strictly read-only observability.

It does NOT add any automation capability. It does not modify crontab, enable or
disable cron, call GitHub discovery, ingest issues, run an executor, run a
validator, publish, push, create a PR, merge, approve, clean up, delete a branch
or worktree, or start a daemon, scheduler loop, webhook, or background worker.
It only parses an existing append-only tick log and reads existing local task
state for human review.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.execution_observability import (
    EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
)
from agent_taskflow.store import TaskMirrorStore


REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SCHEMA_VERSION = (
    "real_scheduled_execution_observability.v1"
)
REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SOURCE = (
    "real_scheduled_execution_observability"
)

# Known scheduler-tick status strings used for recent-tick counting. These match
# the statuses produced by the real one-task automation/scheduler tick.
STATUS_NO_ELIGIBLE_ISSUES = "no_eligible_issues"
STATUS_EXECUTION_COMPLETED = "execution_completed"
STATUS_LOCKED = "locked"

# Backlog statuses surfaced read-only from the local task mirror.
WAITING_APPROVAL_STATUS = "waiting_approval"
BLOCKED_STATUS = "blocked"
QUEUED_STATUS = "queued"

# Ingestion-failure registry status that marks a quarantined record.
QUARANTINED_STATUS = "quarantined"

DEFAULT_RECENT_LIMIT = 20


@dataclass(frozen=True)
class RealScheduledExecutionObservabilityRequest:
    """Inputs for one read-only scheduled-execution observability summary."""

    log_path: Path | None = None
    db_path: Path | None = None
    recent_limit: int = DEFAULT_RECENT_LIMIT

    def __post_init__(self) -> None:
        if self.log_path is not None:
            object.__setattr__(self, "log_path", Path(self.log_path).expanduser())
        if self.db_path is not None:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser())
        if self.recent_limit <= 0:
            raise ValueError("recent_limit must be positive")


def summarize_real_scheduled_execution(
    request: RealScheduledExecutionObservabilityRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> dict[str, Any]:
    """Return a read-only summary of the real scheduled execution path.

    Tolerates an empty, missing, malformed, or partially written log file and a
    missing or foreign database. The returned payload is always ``ok=True``
    because this is observability: it reports what it could read and records
    warnings for anything it could not.
    """

    warnings: list[str] = []

    ticks, malformed_count = _read_tick_log(request.log_path, warnings)
    recent_ticks = _summarize_recent_ticks(
        ticks,
        malformed_count=malformed_count,
        limit=request.recent_limit,
        warnings=warnings,
    )

    last_tick: dict[str, Any] | None = None
    last_tick_observability_summary: dict[str, Any] | None = None
    if ticks:
        latest = ticks[-1]
        last_tick = _summarize_last_tick(latest)
        # The latest tick is always inside the recent window, so any malformed
        # observability_summary warning was already recorded above.
        last_tick_observability_summary, _ = _extract_observability_summary(latest)

    resolved_store = _resolve_store(request, store, warnings)
    backlog = _summarize_backlog(
        resolved_store,
        warnings=warnings,
        limit=request.recent_limit,
    )
    ingestion_failure_registry = _summarize_ingestion_failure_registry(
        resolved_store,
        warnings=warnings,
        limit=request.recent_limit,
    )

    return {
        "ok": True,
        "schema_version": REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SCHEMA_VERSION,
        "source": REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SOURCE,
        "log_path": str(request.log_path) if request.log_path is not None else None,
        "db_path": str(request.db_path) if request.db_path is not None else None,
        "last_tick": last_tick,
        "last_tick_observability_summary": last_tick_observability_summary,
        "last_tick_uses_observability_summary": (
            last_tick_observability_summary is not None
        ),
        "recent_ticks": recent_ticks,
        "backlog": backlog,
        "ingestion_failure_registry": ingestion_failure_registry,
        "warnings": warnings,
        "safety": observability_safety_flags(),
    }


def observability_safety_flags() -> dict[str, bool]:
    """Return the explicit read-only safety flags for this tool."""

    return {
        "read_only": True,
        "cron_modified": False,
        "db_written": False,
        "github_called": False,
        "executor_started": False,
        "validator_started": False,
        "issue_ingested": False,
        "branch_pushed": False,
        "draft_pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "branch_deleted": False,
        "worktree_deleted": False,
        "daemon_started": False,
        "scheduler_loop_started": False,
    }


def _read_tick_log(
    log_path: Path | None,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Parse the JSONL scheduler tick log tolerantly.

    Returns the list of valid JSON object entries plus a count of malformed or
    non-object lines that were skipped. An empty, missing, or partially written
    log file is tolerated and recorded as a warning.
    """

    ticks: list[dict[str, Any]] = []
    malformed_count = 0

    if log_path is None:
        warnings.append("no log_path provided; scheduler tick log was not read")
        return ticks, malformed_count

    if not log_path.exists():
        warnings.append(f"scheduler tick log not found: {log_path}")
        return ticks, malformed_count

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"could not read scheduler tick log: {exc}")
        return ticks, malformed_count

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            malformed_count += 1
            continue
        if not isinstance(parsed, dict):
            malformed_count += 1
            continue
        ticks.append(parsed)

    if malformed_count:
        warnings.append(
            f"skipped {malformed_count} malformed scheduler tick log line(s)"
        )
    if not ticks:
        warnings.append("no valid scheduler tick entries were parsed")

    return ticks, malformed_count


def _summarize_recent_ticks(
    ticks: list[dict[str, Any]],
    *,
    malformed_count: int,
    limit: int,
    warnings: list[str],
) -> dict[str, Any]:
    """Summarize counts over the most recent ``limit`` valid ticks.

    P4-h: when a tick embeds a valid ``observability_summary`` (a normalized
    :class:`~agent_taskflow.execution_observability.UnifiedExecutionSummary`),
    its normalized status is used for the status-based counts. Older logs without
    the field, or ticks whose summary is malformed, fall back to the legacy tick
    ``status`` field, so behavior is unchanged for existing logs.
    """

    window = ticks[-limit:] if limit else list(ticks)

    ok_count = 0
    no_eligible_count = 0
    execution_completed_count = 0
    lock_contention_count = 0
    observability_summary_count = 0
    malformed_observability_summary_count = 0
    statuses: dict[str, int] = {}

    for tick in window:
        summary, summary_malformed = _extract_observability_summary(tick)
        if summary_malformed:
            malformed_observability_summary_count += 1
        if summary is not None:
            observability_summary_count += 1
        status = _effective_status(tick, summary)

        if tick.get("ok") is True:
            ok_count += 1
        if status == STATUS_NO_ELIGIBLE_ISSUES:
            no_eligible_count += 1
        if status == STATUS_EXECUTION_COMPLETED:
            execution_completed_count += 1
        if _is_lock_contention(tick, status):
            lock_contention_count += 1

        key = status if isinstance(status, str) and status else "unknown"
        statuses[key] = statuses.get(key, 0) + 1

    failure_count = len(window) - ok_count

    if malformed_observability_summary_count:
        warnings.append(
            "malformed observability_summary ignored on "
            f"{malformed_observability_summary_count} tick(s); used legacy tick "
            "payload"
        )

    return {
        "limit": limit,
        "total_in_log": len(ticks),
        "total_parsed": len(window),
        "malformed_line_count": malformed_count,
        "observability_summary_count": observability_summary_count,
        "malformed_observability_summary_count": (
            malformed_observability_summary_count
        ),
        "ok_count": ok_count,
        "failure_count": failure_count,
        "no_eligible_count": no_eligible_count,
        "execution_completed_count": execution_completed_count,
        "lock_contention_count": lock_contention_count,
        "statuses": statuses,
    }


def _extract_observability_summary(
    tick: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """Return the embedded ``UnifiedExecutionSummary`` and a malformed flag.

    Scheduler tick logs may embed a JSON-safe ``observability_summary`` when the
    cron tick is run with ``--include-observability-summary`` (P4-g). Older logs
    do not have it, which is the normal case and must keep working.

    Returns ``(summary, False)`` when a valid summary is present, ``(None, True)``
    when an ``observability_summary`` key is present but malformed (not a mapping
    or carrying the wrong schema version), and ``(None, False)`` when the key is
    absent. A malformed summary is never treated as authoritative; the caller
    falls back to the legacy tick payload.
    """

    raw = tick.get("observability_summary")
    if raw is None:
        return None, False
    if (
        isinstance(raw, dict)
        and raw.get("schema_version")
        == EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION
    ):
        return raw, False
    return None, True


def _effective_status(
    tick: dict[str, Any], summary: dict[str, Any] | None
) -> Any:
    """Return the tick status, preferring a valid unified summary status.

    Legacy ticks (no valid ``observability_summary``) fall back to the tick's own
    ``status`` field, preserving existing behavior exactly.
    """

    if summary is not None:
        return summary.get("status")
    return tick.get("status")


def _is_lock_contention(tick: dict[str, Any], status: Any) -> bool:
    if status == STATUS_LOCKED:
        return True
    lock = tick.get("lock")
    return isinstance(lock, dict) and lock.get("contended") is True


def _summarize_last_tick(tick: dict[str, Any]) -> dict[str, Any]:
    """Extract the reviewer-relevant fields from the latest valid tick."""

    safety = tick.get("safety")
    return {
        "mode": tick.get("mode"),
        "status": tick.get("status"),
        "ok": tick.get("ok"),
        "repo": tick.get("repo"),
        "selected_task_key": tick.get("selected_task_key"),
        "selected_issue": _extract_selected_issue(tick),
        "runner_config": _extract_runner_config(tick),
        "publication_config": _extract_publication_config(tick),
        "lock": _extract_lock(tick),
        "safety": safety if isinstance(safety, dict) else {},
    }


def _extract_selected_issue(tick: dict[str, Any]) -> dict[str, Any] | None:
    automation = tick.get("automation")
    selected = (
        automation.get("selected_issue")
        if isinstance(automation, dict)
        else None
    )
    if selected is None:
        selected = tick.get("selected_issue")
    if not isinstance(selected, dict):
        return None
    return {
        "number": selected.get("number"),
        "title": selected.get("title"),
        "url": selected.get("url"),
    }


def _extract_runner_config(tick: dict[str, Any]) -> dict[str, Any] | None:
    runner_config = tick.get("runner_config")
    if not isinstance(runner_config, dict):
        return None
    return {
        "executor": runner_config.get("executor"),
        "model": runner_config.get("model"),
        "validators": runner_config.get("validators"),
        "worktree_root": runner_config.get("worktree_root"),
    }


def _extract_publication_config(tick: dict[str, Any]) -> dict[str, Any] | None:
    publication_config = tick.get("publication_config")
    if not isinstance(publication_config, dict):
        return None
    return {
        "publish_after_execution": publication_config.get("publish_after_execution"),
        "mode": publication_config.get("mode"),
    }


def _extract_lock(tick: dict[str, Any]) -> dict[str, Any]:
    lock = tick.get("lock")
    if not isinstance(lock, dict):
        lock = {}
    return {
        "acquired": lock.get("acquired"),
        "contended": lock.get("contended"),
        "released": lock.get("released"),
    }


def _resolve_store(
    request: RealScheduledExecutionObservabilityRequest,
    store: TaskMirrorStore | None,
    warnings: list[str],
) -> TaskMirrorStore | None:
    if store is not None:
        return store
    if request.db_path is None:
        warnings.append(
            "no db_path provided; backlog and ingestion failure registry "
            "were not read"
        )
        return None
    if not request.db_path.exists():
        warnings.append(f"state DB not found: {request.db_path}")
        return None
    return TaskMirrorStore(request.db_path)


def _summarize_backlog(
    store: TaskMirrorStore | None,
    *,
    warnings: list[str],
    limit: int,
) -> dict[str, Any]:
    empty = {
        "available": False,
        "waiting_approval_count": 0,
        "blocked_count": 0,
        "queued_count": 0,
        "recent_waiting_approval": [],
        "recent_blocked": [],
    }
    if store is None:
        return empty

    try:
        waiting = store.list_tasks(status=WAITING_APPROVAL_STATUS)
        blocked = store.list_tasks(status=BLOCKED_STATUS)
        queued = store.list_tasks(status=QUEUED_STATUS)
    except (sqlite3.Error, OSError, ValueError) as exc:
        warnings.append(f"could not read task backlog: {exc}")
        return empty

    return {
        "available": True,
        "waiting_approval_count": len(waiting),
        "blocked_count": len(blocked),
        "queued_count": len(queued),
        "recent_waiting_approval": [
            {"task_key": task.task_key, "title": task.title}
            for task in waiting[:limit]
        ],
        "recent_blocked": [
            {
                "task_key": task.task_key,
                "title": task.title,
                "blocked_reason": task.blocked_reason,
            }
            for task in blocked[:limit]
        ],
    }


def _summarize_ingestion_failure_registry(*args: object, **kwargs: object) -> dict[str, object]:
    """Read GitHub Issue ingestion failure registry without mutating the DB."""

    import sqlite3
    from pathlib import Path

    warnings = kwargs.get("warnings")
    if not isinstance(warnings, list):
        warnings = None
        for arg in args:
            if isinstance(arg, list):
                warnings = arg
                break

    raw_db_path = kwargs.get("db_path")
    if raw_db_path is None:
        for arg in args:
            candidate = getattr(arg, "db_path", None)
            if candidate is not None:
                raw_db_path = candidate
                break

    if raw_db_path is None:
        for arg in args:
            if isinstance(arg, (str, Path)):
                value = str(arg)
                if value.endswith(".db") or "/" in value:
                    raw_db_path = arg
                    break

    base: dict[str, object] = {
        "available": False,
        "ingestion_failure_count": 0,
        "quarantined_ingestion_failure_count": 0,
        "records": [],
    }

    if raw_db_path is None:
        if warnings is not None:
            warnings.append("ingestion failure registry db_path is unavailable")
        return base

    db_path = Path(raw_db_path).expanduser()
    if not db_path.exists():
        if warnings is not None:
            warnings.append("ingestion failure registry database is missing")
        return base

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            table = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = 'github_issue_ingestion_failures'
                """
            ).fetchone()
            if table is None:
                return {
                    "available": True,
                    "ingestion_failure_count": 0,
                    "quarantined_ingestion_failure_count": 0,
                    "records": [],
                }

            column_rows = conn.execute(
                "PRAGMA table_info(github_issue_ingestion_failures)"
            ).fetchall()
            columns = {str(row["name"]) for row in column_rows}

            rows = conn.execute(
                """
                SELECT *
                FROM github_issue_ingestion_failures
                ORDER BY repo ASC, issue_number ASC
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        if warnings is not None:
            warnings.append(f"failed to read ingestion failure registry: {exc}")
        return base

    records: list[dict[str, object]] = []
    quarantined_count = 0
    for row in rows:
        quarantined = bool(row["quarantined"]) if "quarantined" in columns else False
        if quarantined:
            quarantined_count += 1
        records.append(
            {
                "repo": str(row["repo"]) if "repo" in columns else "",
                "issue_number": int(row["issue_number"]) if "issue_number" in columns else 0,
                "failure_count": int(row["failure_count"]) if "failure_count" in columns else 0,
                "first_failed_at": str(row["first_failed_at"]) if "first_failed_at" in columns else "",
                "last_failed_at": str(row["last_failed_at"]) if "last_failed_at" in columns else "",
                "next_retry_after": (
                    None
                    if "next_retry_after" not in columns or row["next_retry_after"] is None
                    else str(row["next_retry_after"])
                ),
                "quarantined": quarantined,
                "last_error_summary": (
                    str(row["last_error_summary"]) if "last_error_summary" in columns else ""
                ),
            }
        )

    return {
        "available": True,
        "ingestion_failure_count": len(records),
        "quarantined_ingestion_failure_count": quarantined_count,
        "records": records,
    }



def render_real_scheduled_execution_summary(summary: dict[str, Any]) -> str:
    """Render a human-readable view of the observability summary."""

    lines: list[str] = []
    lines.append("Real Scheduled Execution Observability")
    lines.append("======================================")
    lines.append("")
    lines.append(f"Log path: {summary.get('log_path') or '(none)'}")
    lines.append(f"DB path: {summary.get('db_path') or '(none)'}")
    lines.append("")

    last_tick = summary.get("last_tick")
    if not isinstance(last_tick, dict):
        lines.append("Last tick status: (no valid ticks parsed)")
    else:
        lines.append(f"Last tick status: {last_tick.get('status')}")
        lines.append(f"  mode: {last_tick.get('mode')}")
        lines.append(f"  ok: {last_tick.get('ok')}")
        lines.append(f"  selected task: {last_tick.get('selected_task_key') or '(none)'}")
        selected_issue = last_tick.get("selected_issue")
        if isinstance(selected_issue, dict):
            lines.append(
                "  selected issue: "
                f"#{selected_issue.get('number')} {selected_issue.get('title')} "
                f"({selected_issue.get('url')})"
            )
        else:
            lines.append("  selected issue: (none)")
        runner_config = last_tick.get("runner_config")
        if isinstance(runner_config, dict):
            lines.append(
                "  runner: "
                f"executor={runner_config.get('executor')} "
                f"model={runner_config.get('model')} "
                f"validators={runner_config.get('validators')} "
                f"worktree_root={runner_config.get('worktree_root')}"
            )
        publication_config = last_tick.get("publication_config")
        if isinstance(publication_config, dict):
            lines.append(
                "  publication: "
                f"publish_after_execution="
                f"{publication_config.get('publish_after_execution')} "
                f"mode={publication_config.get('mode')}"
            )
        lock = last_tick.get("lock")
        if isinstance(lock, dict):
            lines.append(
                "  lock: "
                f"acquired={lock.get('acquired')} "
                f"contended={lock.get('contended')} "
                f"released={lock.get('released')}"
            )
        if summary.get("last_tick_uses_observability_summary"):
            obs = summary.get("last_tick_observability_summary") or {}
            profile = obs.get("profile") or {}
            lines.append(
                "  observability summary: "
                f"source={obs.get('source')} "
                f"schema_version={obs.get('schema_version')} "
                f"status={obs.get('status')} "
                f"executor={profile.get('executor')} "
                f"model={profile.get('model')}"
            )

    lines.append("")
    recent = summary.get("recent_ticks") or {}
    lines.append(
        f"Recent ticks (limit {recent.get('limit')}, "
        f"parsed {recent.get('total_parsed')}):"
    )
    lines.append(f"  ok: {recent.get('ok_count')}")
    lines.append(f"  failures: {recent.get('failure_count')}")
    lines.append(f"  no_eligible_issues: {recent.get('no_eligible_count')}")
    lines.append(f"  execution_completed: {recent.get('execution_completed_count')}")
    lines.append(f"  lock_contention: {recent.get('lock_contention_count')}")
    lines.append(
        f"  malformed lines skipped: {recent.get('malformed_line_count')}"
    )
    lines.append(
        "  observability summaries read: "
        f"{recent.get('observability_summary_count')}"
    )

    lines.append("")
    backlog = summary.get("backlog") or {}
    lines.append("Backlog:")
    lines.append(f"  waiting_approval: {backlog.get('waiting_approval_count')}")
    lines.append(f"  blocked: {backlog.get('blocked_count')}")
    lines.append(f"  queued: {backlog.get('queued_count')}")

    lines.append("")
    registry = summary.get("ingestion_failure_registry") or {}
    lines.append("Ingestion failure registry:")
    lines.append(
        f"  ingestion failure count: {registry.get('ingestion_failure_count')}"
    )
    lines.append(
        "  quarantined: "
        f"{registry.get('quarantined_ingestion_failure_count')}"
    )

    warnings = summary.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    lines.append("")
    lines.append(
        "Safety: read-only. No cron change, no DB write, no GitHub call, no "
        "executor or validator run, no issue ingest, no push, no PR, no merge, "
        "no approval, no cleanup, no branch/worktree deletion, no daemon or "
        "scheduler loop."
    )
    lines.append("")
    return "\n".join(lines)
