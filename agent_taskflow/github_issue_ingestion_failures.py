"""Durable failure registry for GitHub Issue ingestion.

The registry prevents a single poison issue from blocking later issues forever
when ingestion fails before a task is written into the local mirror. It stores
local operator metadata only. It does not mutate GitHub, start work, approve,
merge, or clean up anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect, default_db_path


INGESTION_FAILURE_REGISTRY_SCHEMA_VERSION = "github_issue_ingestion_failures.v1"
DEFAULT_QUARANTINE_AFTER_FAILURES = 1


@dataclass(frozen=True)
class GitHubIssueIngestionFailureRecord:
    repo: str
    issue_number: int
    failure_count: int
    first_failed_at: str
    last_failed_at: str
    next_retry_after: str | None
    quarantined: bool
    last_error_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "issue_number": self.issue_number,
            "failure_count": self.failure_count,
            "first_failed_at": self.first_failed_at,
            "last_failed_at": self.last_failed_at,
            "next_retry_after": self.next_retry_after,
            "quarantined": self.quarantined,
            "last_error_summary": self.last_error_summary,
        }


class GitHubIssueIngestionFailureRegistry:
    """SQLite-backed registry for failed GitHub Issue ingestion attempts."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else default_db_path()

    def init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_issue_ingestion_failures (
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    next_retry_after TEXT,
                    quarantined INTEGER NOT NULL DEFAULT 0,
                    last_error_summary TEXT NOT NULL,
                    PRIMARY KEY(repo, issue_number)
                )
                """
            )
            column_rows = conn.execute(
                "PRAGMA table_info(github_issue_ingestion_failures)"
            ).fetchall()
            columns = {str(row["name"]) for row in column_rows}
            if "next_retry_after" not in columns:
                conn.execute(
                    "ALTER TABLE github_issue_ingestion_failures "
                    "ADD COLUMN next_retry_after TEXT"
                )
            if "quarantined" not in columns:
                conn.execute(
                    "ALTER TABLE github_issue_ingestion_failures "
                    "ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0"
                )

    def record_failure(
        self,
        *,
        repo: str,
        issue_number: int,
        error_summary: str,
        quarantine_after_failures: int = DEFAULT_QUARANTINE_AFTER_FAILURES,
    ) -> GitHubIssueIngestionFailureRecord:
        repo = _normalize_repo(repo)
        issue_number = _positive_issue_number(issue_number)
        threshold = max(1, int(quarantine_after_failures))
        now = utc_now_iso()
        summary = _error_summary(error_summary)
        self.init_db()

        with connect(self.db_path) as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM github_issue_ingestion_failures
                WHERE repo = ? AND issue_number = ?
                """,
                (repo, issue_number),
            ).fetchone()
            if existing is None:
                failure_count = 1
                first_failed_at = now
            else:
                failure_count = int(existing["failure_count"]) + 1
                first_failed_at = str(existing["first_failed_at"])

            quarantined = failure_count >= threshold
            conn.execute(
                """
                INSERT INTO github_issue_ingestion_failures (
                    repo,
                    issue_number,
                    failure_count,
                    first_failed_at,
                    last_failed_at,
                    next_retry_after,
                    quarantined,
                    last_error_summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, issue_number) DO UPDATE SET
                    failure_count = excluded.failure_count,
                    last_failed_at = excluded.last_failed_at,
                    next_retry_after = excluded.next_retry_after,
                    quarantined = excluded.quarantined,
                    last_error_summary = excluded.last_error_summary
                """,
                (
                    repo,
                    issue_number,
                    failure_count,
                    first_failed_at,
                    now,
                    None,
                    1 if quarantined else 0,
                    summary,
                ),
            )

        record = self.get_failure(repo=repo, issue_number=issue_number)
        if record is None:  # pragma: no cover - defensive guard after upsert
            raise RuntimeError("failed to read recorded ingestion failure")
        return record

    def clear_failure(self, *, repo: str, issue_number: int) -> bool:
        repo = _normalize_repo(repo)
        issue_number = _positive_issue_number(issue_number)
        self.init_db()
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                DELETE FROM github_issue_ingestion_failures
                WHERE repo = ? AND issue_number = ?
                """,
                (repo, issue_number),
            )
            return cur.rowcount > 0

    def get_failure(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> GitHubIssueIngestionFailureRecord | None:
        repo = _normalize_repo(repo)
        issue_number = _positive_issue_number(issue_number)
        if not Path(self.db_path).exists():
            return None
        self.init_db()
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM github_issue_ingestion_failures
                WHERE repo = ? AND issue_number = ?
                """,
                (repo, issue_number),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_failures(
        self,
        *,
        repo: str | None = None,
        active_only: bool = False,
    ) -> list[GitHubIssueIngestionFailureRecord]:
        if not Path(self.db_path).exists():
            return []
        self.init_db()
        clauses: list[str] = []
        params: list[Any] = []
        if repo is not None:
            clauses.append("repo = ?")
            params.append(_normalize_repo(repo))
        if active_only:
            clauses.append("quarantined = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM github_issue_ingestion_failures
                {where}
                ORDER BY repo ASC, issue_number ASC
                """,
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def active_issue_numbers(self, *, repo: str) -> set[int]:
        return {
            record.issue_number
            for record in self.list_failures(repo=repo, active_only=True)
        }

    def summary(self, *, repo: str | None = None) -> dict[str, int]:
        records = self.list_failures(repo=repo, active_only=False)
        return {
            "ingestion_failure_count": len(records),
            "quarantined_ingestion_failure_count": sum(
                1 for record in records if record.quarantined
            ),
        }


def apply_ingestion_failure_filter(
    discovery: dict[str, Any],
    *,
    registry: GitHubIssueIngestionFailureRegistry,
    repo: str,
) -> dict[str, Any]:
    """Move quarantined candidates out of recommended_candidates."""

    quarantined_numbers = registry.active_issue_numbers(repo=repo)
    failures_by_number = {
        record.issue_number: record
        for record in registry.list_failures(repo=repo, active_only=True)
    }
    if not quarantined_numbers:
        summary = dict(discovery.get("summary") or {})
        summary.update(registry.summary(repo=repo))
        return {**discovery, "summary": summary}

    recommended: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = list(discovery.get("quarantined_ingestion") or [])
    for candidate in discovery.get("recommended_candidates") or []:
        issue_number = _coerce_issue_number(candidate.get("number"))
        if issue_number not in quarantined_numbers:
            recommended.append(candidate)
            continue
        record = failures_by_number[issue_number]
        quarantined.append(
            {
                **candidate,
                "reason": "issue ingestion is quarantined after failure",
                "ingestion_failure": record.to_dict(),
            }
        )

    new_issues = [
        issue
        for issue in discovery.get("new_issues") or []
        if _coerce_issue_number(issue.get("number")) not in quarantined_numbers
    ]
    summary = dict(discovery.get("summary") or {})
    summary["new_issue_count"] = len(new_issues)
    summary["recommended_candidate_count"] = len(recommended)
    summary["quarantined_ingestion_count"] = len(quarantined)
    summary.update(registry.summary(repo=repo))

    return {
        **discovery,
        "new_issues": new_issues,
        "recommended_candidates": recommended,
        "quarantined_ingestion": quarantined,
        "summary": summary,
    }


def failure_registry_payload(
    registry: GitHubIssueIngestionFailureRegistry,
    *,
    repo: str,
) -> dict[str, Any]:
    records = [record.to_dict() for record in registry.list_failures(repo=repo)]
    return {
        "schema_version": INGESTION_FAILURE_REGISTRY_SCHEMA_VERSION,
        "records": records,
        "summary": registry.summary(repo=repo),
    }


def _row_to_record(row: sqlite3.Row) -> GitHubIssueIngestionFailureRecord:
    return GitHubIssueIngestionFailureRecord(
        repo=str(row["repo"]),
        issue_number=int(row["issue_number"]),
        failure_count=int(row["failure_count"]),
        first_failed_at=str(row["first_failed_at"]),
        last_failed_at=str(row["last_failed_at"]),
        next_retry_after=(
            str(row["next_retry_after"]) if row["next_retry_after"] else None
        ),
        quarantined=bool(row["quarantined"]),
        last_error_summary=str(row["last_error_summary"]),
    )


def _normalize_repo(repo: str) -> str:
    value = str(repo or "").strip()
    if "/" not in value or value.startswith("/") or value.endswith("/"):
        raise ValueError("repo must be in owner/name form")
    return value


def _positive_issue_number(issue_number: int) -> int:
    parsed = int(issue_number)
    if parsed <= 0:
        raise ValueError("issue_number must be positive")
    return parsed


def _coerce_issue_number(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _error_summary(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:500] or "unknown ingestion failure"


__all__ = [
    "DEFAULT_QUARANTINE_AFTER_FAILURES",
    "GitHubIssueIngestionFailureRecord",
    "GitHubIssueIngestionFailureRegistry",
    "INGESTION_FAILURE_REGISTRY_SCHEMA_VERSION",
    "apply_ingestion_failure_filter",
    "failure_registry_payload",
]
