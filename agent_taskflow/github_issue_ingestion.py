"""Read-only GitHub Issue ingestion into the local task mirror."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.models import TaskRecord, require_absolute_path, utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


ISSUE_SPEC_FILENAME = "issue_spec.md"
INGESTION_EVENT_TYPE = "github_issue_ingested"
INGESTION_SOURCE = "github"


class GitHubIssueIngestionError(RuntimeError):
    """Raised when GitHub Issue ingestion cannot continue."""


@dataclass(frozen=True)
class GitHubIssueSnapshot:
    """Read-only snapshot of a single GitHub Issue."""

    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    author: str | None
    url: str | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GitHubIssueSnapshot":
        try:
            number = int(data["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("issue JSON must include numeric number") from exc

        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("issue JSON must include non-empty title")

        labels = tuple(_label_names(data.get("labels")))
        author = _author_login(data.get("author"))
        return cls(
            number=number,
            title=title,
            body=str(data.get("body") or ""),
            state=str(data.get("state") or "").strip().lower(),
            labels=labels,
            author=author,
            url=str(data["url"]) if data.get("url") else None,
            created_at=str(data["createdAt"]) if data.get("createdAt") else None,
            updated_at=str(data["updatedAt"]) if data.get("updatedAt") else None,
        )


@dataclass(frozen=True)
class GitHubIssueIngestionRequest:
    """Request for local-only GitHub Issue ingestion."""

    repo: str
    issue_number: int
    local_repo_path: Path
    artifact_root: Path | None = None
    task_key: str | None = None
    project: str | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        repo = self.repo.strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise ValueError("repo must be in owner/name form")
        object.__setattr__(self, "repo", repo)

        if self.issue_number <= 0:
            raise ValueError("issue_number must be positive")

        local_repo_path = require_absolute_path(self.local_repo_path, "local_repo_path")
        if not local_repo_path.is_dir():
            raise ValueError(f"local_repo_path must be an existing directory: {local_repo_path}")
        object.__setattr__(self, "local_repo_path", local_repo_path)

        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                require_absolute_path(self.artifact_root, "artifact_root"),
            )

        if self.task_key is not None:
            object.__setattr__(self, "task_key", normalize_task_key(self.task_key))


@dataclass(frozen=True)
class GitHubIssueIngestionResult:
    """Structured result for GitHub Issue ingestion."""

    ok: bool
    status: str
    task_key: str
    repo: str
    issue_number: int
    issue_url: str | None
    title: str
    issue_state: str
    local_repo_path: Path
    artifact_dir: Path
    issue_spec_path: Path
    db_path: Path
    wrote_task: bool
    wrote_artifact: bool
    recorded_event: bool
    summary: str


IssueFetcher = Callable[[str, int], GitHubIssueSnapshot]


def fetch_issue_with_gh(repo: str, issue_number: int) -> GitHubIssueSnapshot:
    """Fetch a GitHub Issue through the read-only gh issue view command."""

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
        raise GitHubIssueIngestionError(
            f"gh issue view failed with {completed.returncode}: {completed.stderr.strip()}"
        )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubIssueIngestionError("gh issue view returned invalid JSON") from exc

    if not isinstance(data, dict):
        raise GitHubIssueIngestionError("gh issue view returned non-object JSON")

    try:
        return GitHubIssueSnapshot.from_json(data)
    except ValueError as exc:
        raise GitHubIssueIngestionError(str(exc)) from exc


def ingest_github_issue(
    request: GitHubIssueIngestionRequest,
    *,
    store: TaskMirrorStore,
    fetcher: IssueFetcher = fetch_issue_with_gh,
) -> GitHubIssueIngestionResult:
    """Mirror one GitHub Issue into the local store without dispatching."""

    try:
        issue = fetcher(request.repo, request.issue_number)
    except Exception as exc:
        if isinstance(exc, GitHubIssueIngestionError):
            raise
        raise GitHubIssueIngestionError(str(exc)) from exc

    task_key = request.task_key or normalize_task_key(f"AT-GH-{issue.number}")
    project = request.project or _project_from_repo(request.repo)
    artifact_root = request.artifact_root or request.local_repo_path / ".agent-taskflow" / "artifacts"
    artifact_dir = artifact_root / task_key
    issue_spec_path = artifact_dir / ISSUE_SPEC_FILENAME
    task_status = _task_status_from_issue_state(issue.state)
    blocked_reason = (
        "GitHub issue is closed; ingestion is read-only and does not make it runnable."
        if task_status == "blocked"
        else None
    )

    if request.dry_run:
        return GitHubIssueIngestionResult(
            ok=True,
            status="dry_run",
            task_key=task_key,
            repo=request.repo,
            issue_number=issue.number,
            issue_url=issue.url,
            title=issue.title,
            issue_state=issue.state,
            local_repo_path=request.local_repo_path,
            artifact_dir=artifact_dir,
            issue_spec_path=issue_spec_path,
            db_path=store.db_path,
            wrote_task=False,
            wrote_artifact=False,
            recorded_event=False,
            summary="Dry run completed; no local state was written.",
        )

    store.init_db()
    existing = store.get_task(task_key)
    would_write_task = existing is None
    summary_status = "ingested" if existing is None else "reused"

    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=project,
            board=project,
            title=issue.title,
            status=task_status,
            repo_path=request.local_repo_path,
            artifact_dir=artifact_dir,
            blocked_reason=blocked_reason,
        )
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    issue_spec_path.write_text(
        render_issue_spec(
            repo=request.repo,
            task_key=task_key,
            issue=issue,
            ingested_at=utc_now_iso(),
        ),
        encoding="utf-8",
    )
    _record_artifact_once(store, task_key, issue_spec_path)
    store.record_task_event(
        task_key,
        INGESTION_EVENT_TYPE,
        INGESTION_SOURCE,
        message="GitHub issue ingested",
        payload={
            "kind": INGESTION_EVENT_TYPE,
            "repo": request.repo,
            "issue_number": issue.number,
            "issue_url": issue.url,
            "issue_state": issue.state,
            "labels": list(issue.labels),
            "author": issue.author,
            "dry_run": False,
        },
    )

    return GitHubIssueIngestionResult(
        ok=True,
        status=summary_status,
        task_key=task_key,
        repo=request.repo,
        issue_number=issue.number,
        issue_url=issue.url,
        title=issue.title,
        issue_state=issue.state,
        local_repo_path=request.local_repo_path,
        artifact_dir=artifact_dir,
        issue_spec_path=issue_spec_path,
        db_path=store.db_path,
        wrote_task=would_write_task,
        wrote_artifact=True,
        recorded_event=True,
        summary=(
            "GitHub issue ingested into local task mirror."
            if would_write_task
            else "Existing task reused; issue artifact and event refreshed."
        ),
    )


def render_issue_spec(
    *,
    repo: str,
    task_key: str,
    issue: GitHubIssueSnapshot,
    ingested_at: str,
) -> str:
    """Render the issue snapshot as local input/spec evidence."""

    labels = ", ".join(issue.labels) if issue.labels else "(none)"
    body = issue.body if issue.body else "(empty)"
    return "\n".join(
        [
            "# GitHub Issue Spec",
            "",
            "This artifact is input/spec evidence mirrored from GitHub. It is not",
            "implementation evidence, validation evidence, approval, PR creation,",
            "push, merge, or cleanup evidence.",
            "",
            f"- Task key: {task_key}",
            f"- Repository: {repo}",
            f"- Issue number: {issue.number}",
            f"- Issue URL: {issue.url or '(none)'}",
            f"- Issue state: {issue.state}",
            f"- Title: {issue.title}",
            f"- Labels: {labels}",
            f"- Author: {issue.author or '(unknown)'}",
            f"- Created at: {issue.created_at or '(unknown)'}",
            f"- Updated at: {issue.updated_at or '(unknown)'}",
            f"- Ingested at: {ingested_at}",
            "",
            "## Body",
            "",
            body,
            "",
        ]
    )


def ingestion_result_to_dict(result: GitHubIssueIngestionResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "status": result.status,
        "task_key": result.task_key,
        "repo": result.repo,
        "issue_number": result.issue_number,
        "issue_url": result.issue_url,
        "title": result.title,
        "issue_state": result.issue_state,
        "local_repo_path": str(result.local_repo_path),
        "artifact_dir": str(result.artifact_dir),
        "issue_spec_path": str(result.issue_spec_path),
        "db_path": str(result.db_path),
        "wrote_task": result.wrote_task,
        "wrote_artifact": result.wrote_artifact,
        "recorded_event": result.recorded_event,
        "summary": result.summary,
    }


def _project_from_repo(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def _task_status_from_issue_state(state: str) -> str:
    return "queued" if state.lower() == "open" else "blocked"


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


def _author_login(raw_author: Any) -> str | None:
    if isinstance(raw_author, str):
        return raw_author or None
    if isinstance(raw_author, dict):
        login = str(raw_author.get("login") or "").strip()
        return login or None
    return None


def _record_artifact_once(
    store: TaskMirrorStore,
    task_key: str,
    issue_spec_path: Path,
) -> None:
    existing_paths = {artifact.path for artifact in store.list_task_artifacts(task_key)}
    if issue_spec_path not in existing_paths:
        store.record_task_artifact(task_key, "issue_spec", issue_spec_path)
