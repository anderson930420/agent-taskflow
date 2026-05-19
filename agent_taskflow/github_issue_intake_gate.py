"""Deterministic GitHub Issue intake gate into the local task mirror.

This module only mirrors selected GitHub Issues into SQLite. It does not write
issue-spec artifacts, create worktrees, run executors, run validators, push
branches, or create PRs.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot
from agent_taskflow.models import TaskRecord, require_absolute_path
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key


INGESTION_EVENT_TYPE = "github_issue_ingested"
INGESTION_SOURCE = "github_issue_intake"
TASK_KEY_PREFIX = "GH"


class GitHubIssueIntakeError(RuntimeError):
    """Raised when selected GitHub Issue intake cannot continue."""


@dataclass(frozen=True)
class GitHubIssueIntakeRequest:
    """Request for deterministic selected GitHub Issue intake."""

    repo: str
    issue_numbers: tuple[int, ...]
    repo_path: Path
    artifact_root: Path
    project: str | None = None
    board: str | None = None
    db_path: Path | None = None
    dry_run: bool = True

    def __post_init__(self) -> None:
        repo = self.repo.strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise ValueError("repo must be in owner/name form")
        object.__setattr__(self, "repo", repo)

        normalized_numbers = _normalize_issue_numbers(self.issue_numbers)
        if not normalized_numbers:
            raise ValueError("issue_numbers must not be empty")
        object.__setattr__(self, "issue_numbers", normalized_numbers)

        project = _normalized_text(self.project) or self.repo.rsplit("/", 1)[-1]
        board = _normalized_text(self.board) or project
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "board", board)

        repo_path = require_absolute_path(self.repo_path, "repo_path")
        if not repo_path.is_dir():
            raise ValueError(f"repo_path must be an existing directory: {repo_path}")
        object.__setattr__(self, "repo_path", repo_path)

        artifact_root = require_absolute_path(self.artifact_root, "artifact_root")
        object.__setattr__(self, "artifact_root", artifact_root)

        if self.db_path is None:
            db_path = default_db_path()
        else:
            db_path = require_absolute_path(self.db_path, "db_path")
        object.__setattr__(self, "db_path", Path(db_path))


IssueFetcher = Callable[[str, int], GitHubIssueSnapshot]


@dataclass(frozen=True)
class LocalIssueIntakeMatch:
    """Local evidence that a GitHub Issue has already been intake-mirrored."""

    issue_number: int
    task_key: str
    title: str | None
    status: str | None


def fetch_issue_with_gh(repo: str, issue_number: int) -> GitHubIssueSnapshot:
    """Fetch one GitHub Issue through the read-only gh issue view command."""

    completed = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,state,labels,author,url,updatedAt,createdAt",
        ],
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise GitHubIssueIntakeError(
            f"gh issue view failed with {completed.returncode}: {completed.stderr.strip()}"
        )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubIssueIntakeError("gh issue view returned invalid JSON") from exc

    if not isinstance(data, dict):
        raise GitHubIssueIntakeError("gh issue view returned non-object JSON")

    try:
        return GitHubIssueSnapshot.from_json(data)
    except ValueError as exc:
        raise GitHubIssueIntakeError(str(exc)) from exc


def intake_selected_github_issues(
    request: GitHubIssueIntakeRequest,
    *,
    store: TaskMirrorStore | None = None,
    fetcher: IssueFetcher = fetch_issue_with_gh,
) -> dict[str, Any]:
    """Mirror only the explicitly selected GitHub Issues into SQLite."""

    current_store = store or TaskMirrorStore(request.db_path)
    local_matches = read_local_issue_intake_matches(request.db_path, repo=request.repo)

    selected: list[dict[str, Any]] = []
    wrote = False
    initialized_db = False

    for issue_number in request.issue_numbers:
        task_key = _task_key_for_issue(issue_number)
        try:
            issue = fetcher(request.repo, issue_number)
        except Exception as exc:
            selected.append(
                {
                    "issue_number": issue_number,
                    "task_key": task_key,
                    "title": None,
                    "action": "failed",
                    "status": None,
                    "issue_url": None,
                    "error": str(exc),
                }
            )
            continue

        existing = local_matches.get(issue.number)
        if existing is not None:
            selected.append(
                {
                    "issue_number": issue.number,
                    "task_key": existing.task_key,
                    "title": existing.title or issue.title,
                    "action": "already_ingested",
                    "status": existing.status or "queued",
                    "issue_url": issue.url,
                }
            )
            continue

        if request.dry_run:
            selected.append(
                {
                    "issue_number": issue.number,
                    "task_key": task_key,
                    "title": issue.title,
                    "action": "would_ingest",
                    "status": "queued",
                    "issue_url": issue.url,
                }
            )
            continue

        if not initialized_db:
            current_store.init_db()
            initialized_db = True

        artifact_dir = request.artifact_root / task_key
        current_store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=request.project or "",
                board=request.board,
                title=issue.title,
                status="queued",
                repo_path=request.repo_path,
                artifact_dir=artifact_dir,
            )
        )
        current_store.record_task_event(
            task_key,
            INGESTION_EVENT_TYPE,
            INGESTION_SOURCE,
            message="GitHub issue ingested",
            payload={
                "kind": INGESTION_EVENT_TYPE,
                "repo": request.repo,
                "issue_number": issue.number,
                "issue_url": issue.url,
                "title": issue.title,
                "task_key": task_key,
                "status": "queued",
            },
        )
        selected.append(
            {
                "issue_number": issue.number,
                "task_key": task_key,
                "title": issue.title,
                "action": "ingested",
                "status": "queued",
                "issue_url": issue.url,
            }
        )
        wrote = True
        local_matches[issue.number] = LocalIssueIntakeMatch(
            issue_number=issue.number,
            task_key=task_key,
            title=issue.title,
            status="queued",
        )

    failed_count = sum(item["action"] == "failed" for item in selected)
    mode = "dry_run" if request.dry_run else "confirmed"
    return {
        "ok": failed_count == 0,
        "mode": mode,
        "repo": request.repo,
        "project": request.project,
        "board": request.board,
        "repo_path": str(request.repo_path),
        "artifact_root": str(request.artifact_root),
        "db_path": str(request.db_path),
        "selected_issue_numbers": list(request.issue_numbers),
        "selected": selected,
        "written": wrote,
        "summary": {
            "selected_count": len(request.issue_numbers),
            "ingested_count": sum(item["action"] == "ingested" for item in selected),
            "already_ingested_count": sum(item["action"] == "already_ingested" for item in selected),
            "would_ingest_count": sum(item["action"] == "would_ingest" for item in selected),
            "failed_count": failed_count,
        },
        "safety": {
            "read_only": request.dry_run,
            "db_written": wrote,
            "task_worktrees_written": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
        },
    }


def read_local_issue_intake_matches(
    db_path: Path,
    *,
    repo: str,
) -> dict[int, LocalIssueIntakeMatch]:
    """Read already-ingested GitHub Issue mappings without creating the DB."""

    path = Path(db_path).expanduser()
    if not path.exists():
        return {}

    try:
        conn = sqlite3.connect(_sqlite_read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise GitHubIssueIntakeError(f"could not open DB read-only: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn, "tasks"):
            return {}

        matches: dict[int, LocalIssueIntakeMatch] = {}
        task_rows = conn.execute(
            """
            SELECT task_key, title, status
            FROM tasks
            ORDER BY task_key ASC
            """
        ).fetchall()
        task_meta = {
            str(row["task_key"]): {
                "title": row["title"],
                "status": row["status"],
            }
            for row in task_rows
        }
        for task_key, meta in task_meta.items():
            issue_number = _issue_number_from_task_key(task_key)
            if issue_number is None:
                continue
            matches[issue_number] = LocalIssueIntakeMatch(
                issue_number=issue_number,
                task_key=task_key,
                title=meta["title"],
                status=meta["status"],
            )

        if _has_table(conn, "task_events"):
            rows = conn.execute(
                """
                SELECT task_key, payload_json
                FROM task_events
                WHERE event_type = ?
                ORDER BY id ASC
                """,
                (INGESTION_EVENT_TYPE,),
            ).fetchall()
            for row in rows:
                payload = _json_object(row["payload_json"])
                if payload.get("repo") != repo:
                    continue
                issue_number = _positive_int(payload.get("issue_number"))
                if issue_number is None:
                    continue
                task_key = str(row["task_key"])
                matches[issue_number] = LocalIssueIntakeMatch(
                    issue_number=issue_number,
                    task_key=task_key,
                    title=_task_title_for_key(task_meta, task_key) or _normalized_text(payload.get("title")),
                    status=_normalized_text(payload.get("status")) or _task_status_for_key(task_meta, task_key),
                )

        return matches
    except sqlite3.Error as exc:
        raise GitHubIssueIntakeError(f"could not read DB: {exc}") from exc
    finally:
        conn.close()


def _task_key_for_issue(issue_number: int) -> str:
    return normalize_task_key(f"{TASK_KEY_PREFIX}-{issue_number}")


def _normalize_issue_numbers(issue_numbers: tuple[int, ...]) -> tuple[int, ...]:
    seen: set[int] = set()
    normalized: list[int] = []
    for issue_number in issue_numbers:
        if issue_number <= 0:
            raise ValueError("issue_numbers must contain positive integers")
        if issue_number in seen:
            continue
        seen.add(issue_number)
        normalized.append(issue_number)
    return tuple(normalized)


def _normalized_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _issue_number_from_task_key(task_key: str) -> int | None:
    for pattern in (r"GH-(\d+)", r"AT-GH-(\d+)"):
        match = re.fullmatch(pattern, task_key)
        if match is not None:
            return _positive_int(match.group(1))
    return None


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


def _task_title_for_key(task_meta: dict[str, dict[str, Any]], task_key: str) -> str | None:
    meta = task_meta.get(task_key)
    if meta is None:
        return None
    return _normalized_text(meta.get("title"))


def _task_status_for_key(task_meta: dict[str, dict[str, Any]], task_key: str) -> str | None:
    meta = task_meta.get(task_key)
    if meta is None:
        return None
    return _normalized_text(meta.get("status"))


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
