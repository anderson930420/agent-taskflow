"""Read-only GitHub Issue discovery against the local task mirror."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from agent_taskflow.store import default_db_path


BLOCKED_LABELS = frozenset(
    {
        "blocked",
        "do-not-run",
        "no-agent",
        "invalid",
        "wontfix",
        "duplicate",
    }
)

SAFETY_BLOCK = {
    "read_only": True,
    "ingested": False,
    "db_written": False,
    "workspace_prepared": False,
    "executor_started": False,
    "branch_pushed": False,
    "pr_created": False,
    "merged": False,
    "approved": False,
    "cleanup_performed": False,
}


class GitHubIssueDiscoveryError(RuntimeError):
    """Raised when read-only GitHub Issue discovery cannot continue."""


@dataclass(frozen=True)
class GitHubIssueDiscoveryRequest:
    """Request for one-shot read-only GitHub Issue discovery."""

    repo: str
    db_path: Path | None = None
    state: str = "open"
    limit: int = 100
    include_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        repo = self.repo.strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise ValueError("repo must be in owner/name form")
        object.__setattr__(self, "repo", repo)

        state = self.state.strip().lower()
        if state not in {"open", "closed", "all"}:
            raise ValueError("state must be one of: open, closed, all")
        object.__setattr__(self, "state", state)

        if self.limit <= 0:
            raise ValueError("limit must be positive")

        db_path = self.db_path or default_db_path()
        object.__setattr__(self, "db_path", Path(db_path).expanduser())
        object.__setattr__(
            self,
            "include_labels",
            tuple(_normalize_label(label) for label in self.include_labels),
        )
        object.__setattr__(
            self,
            "exclude_labels",
            tuple(_normalize_label(label) for label in self.exclude_labels),
        )


@dataclass(frozen=True)
class GitHubIssueDiscoveryIssue:
    """Read-only snapshot of a GitHub Issue for discovery."""

    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GitHubIssueDiscoveryIssue":
        try:
            number = int(data["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("issue JSON must include numeric number") from exc

        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("issue JSON must include non-empty title")

        return cls(
            number=number,
            title=title,
            state=str(data.get("state") or "").strip().lower(),
            labels=tuple(_label_names(data.get("labels"))),
            url=str(data["url"]) if data.get("url") else None,
            created_at=str(data["createdAt"]) if data.get("createdAt") else None,
            updated_at=str(data["updatedAt"]) if data.get("updatedAt") else None,
        )


@dataclass(frozen=True)
class LocalIssueMatch:
    """Local mirror evidence that a GitHub Issue has already been ingested."""

    issue_number: int
    task_key: str
    title: str | None = None


IssueListFetcher = Callable[[GitHubIssueDiscoveryRequest], list[GitHubIssueDiscoveryIssue]]


def fetch_issues_with_gh(
    request: GitHubIssueDiscoveryRequest,
) -> list[GitHubIssueDiscoveryIssue]:
    """Fetch GitHub Issues through the read-only gh issue list command."""

    completed = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            request.repo,
            "--state",
            request.state,
            "--limit",
            str(request.limit),
            "--json",
            "number,title,state,labels,url,updatedAt,createdAt",
        ],
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise GitHubIssueDiscoveryError(
            f"gh issue list failed with {completed.returncode}: {completed.stderr.strip()}"
        )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubIssueDiscoveryError("gh issue list returned invalid JSON") from exc

    if not isinstance(data, list):
        raise GitHubIssueDiscoveryError("gh issue list returned non-list JSON")

    try:
        return [GitHubIssueDiscoveryIssue.from_json(item) for item in data]
    except (TypeError, ValueError) as exc:
        raise GitHubIssueDiscoveryError(str(exc)) from exc


def discover_github_issues(
    request: GitHubIssueDiscoveryRequest,
    *,
    fetcher: IssueListFetcher = fetch_issues_with_gh,
) -> dict[str, Any]:
    """Classify GitHub Issues without writing local state or starting work."""

    try:
        issues = fetcher(request)
    except GitHubIssueDiscoveryError:
        raise
    except Exception as exc:
        raise GitHubIssueDiscoveryError(str(exc)) from exc

    local_matches = read_local_issue_matches(request.db_path, repo=request.repo)

    new_issues: list[dict[str, Any]] = []
    already_ingested: list[dict[str, Any]] = []
    closed_or_blocked: list[dict[str, Any]] = []
    not_eligible: list[dict[str, Any]] = []
    recommended_candidates: list[dict[str, Any]] = []

    for issue in sorted(issues, key=lambda item: item.number):
        match = local_matches.get(issue.number)
        labels = {_normalize_label(label) for label in issue.labels}
        include_missing = [
            label for label in request.include_labels if label and label not in labels
        ]
        exclude_present = [
            label for label in request.exclude_labels if label and label in labels
        ]

        if match is not None:
            already_ingested.append(
                {
                    "number": issue.number,
                    "title": issue.title,
                    "task_key": match.task_key,
                    "reason": "matching issue already exists in local task mirror",
                }
            )
            continue

        base = _issue_to_dict(issue)
        if issue.state != "open":
            closed_or_blocked.append(
                {
                    **base,
                    "reason": "issue state is not open",
                }
            )
            continue

        blocked_labels = sorted(labels & BLOCKED_LABELS)
        if blocked_labels:
            closed_or_blocked.append(
                {
                    **base,
                    "reason": "issue has blocked label",
                    "blocked_labels": blocked_labels,
                }
            )
            continue

        if include_missing:
            not_eligible.append(
                {
                    **base,
                    "reason": "issue is missing required include label",
                    "missing_labels": include_missing,
                }
            )
            continue

        if exclude_present:
            not_eligible.append(
                {
                    **base,
                    "reason": "issue has excluded label",
                    "excluded_labels": exclude_present,
                }
            )
            continue

        candidate = {
            **base,
            "reason": "open issue not found in local task mirror",
        }
        new_issues.append(candidate)
        recommended_candidates.append(candidate)

    summary = {
        "new_issue_count": len(new_issues),
        "already_ingested_count": len(already_ingested),
        "closed_or_blocked_count": len(closed_or_blocked),
        "not_eligible_count": len(not_eligible),
        "recommended_candidate_count": len(recommended_candidates),
    }

    return {
        "ok": True,
        "status": "discovered",
        "repo": request.repo,
        "new_issues": new_issues,
        "already_ingested": already_ingested,
        "closed_or_blocked": closed_or_blocked,
        "not_eligible": not_eligible,
        "recommended_candidates": recommended_candidates,
        "summary": summary,
        "safety": dict(SAFETY_BLOCK),
    }


def read_local_issue_matches(
    db_path: Path,
    *,
    repo: str,
) -> dict[int, LocalIssueMatch]:
    """Read already-ingested GitHub Issue mappings without opening DB read/write."""

    path = Path(db_path).expanduser()
    if not path.exists():
        return {}

    try:
        conn = sqlite3.connect(_sqlite_read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise GitHubIssueDiscoveryError(f"could not open DB read-only: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn, "tasks"):
            return {}

        task_rows = conn.execute(
            """
            SELECT task_key, title
            FROM tasks
            ORDER BY task_key ASC
            """
        ).fetchall()
        matches: dict[int, LocalIssueMatch] = {}
        task_titles = {str(row["task_key"]): row["title"] for row in task_rows}

        for task_key, title in task_titles.items():
            issue_number = _issue_number_from_task_key(task_key)
            if issue_number is not None:
                matches.setdefault(
                    issue_number,
                    LocalIssueMatch(issue_number=issue_number, task_key=task_key, title=title),
                )

        if _has_table(conn, "task_events"):
            rows = conn.execute(
                """
                SELECT task_key, payload_json
                FROM task_events
                WHERE event_type = 'github_issue_ingested'
                ORDER BY id ASC
                """
            ).fetchall()
            for row in rows:
                payload = _json_object(row["payload_json"])
                if payload.get("repo") != repo:
                    continue
                issue_number = _positive_int(payload.get("issue_number"))
                if issue_number is None:
                    continue
                task_key = str(row["task_key"])
                matches[issue_number] = LocalIssueMatch(
                    issue_number=issue_number,
                    task_key=task_key,
                    title=task_titles.get(task_key),
                )

        return matches
    except sqlite3.Error as exc:
        raise GitHubIssueDiscoveryError(f"could not read DB: {exc}") from exc
    finally:
        conn.close()


def _issue_to_dict(issue: GitHubIssueDiscoveryIssue) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "state": issue.state,
        "labels": list(issue.labels),
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "url": issue.url,
    }


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
    return _positive_int(match.group(1))


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _label_names(raw_labels: Any) -> list[str]:
    if not isinstance(raw_labels, list):
        return []
    names: list[str] = []
    for item in raw_labels:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("name") or "").strip()
        else:
            value = ""
        if value:
            names.append(value)
    return names


def _normalize_label(label: str) -> str:
    return str(label or "").strip().lower()
