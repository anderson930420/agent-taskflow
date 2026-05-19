"""Selected GitHub Issue intake into the local task mirror."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.artifacts import artifact_dir_for
from agent_taskflow.github_issue_discovery import BLOCKED_LABELS, LocalIssueMatch, read_local_issue_matches
from agent_taskflow.github_issue_ingestion import (
    GitHubIssueSnapshot,
    ISSUE_SPEC_FILENAME,
    INGESTION_EVENT_TYPE,
    INGESTION_SOURCE,
    render_issue_spec,
)
from agent_taskflow.models import TaskRecord, require_absolute_path, utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


DEFAULT_TASK_KEY_PREFIX = "AT-GH"


class GitHubIssueIntakeError(RuntimeError):
    """Raised when selected GitHub Issue intake cannot continue."""


@dataclass(frozen=True)
class GitHubIssueIntakeRequest:
    """Request for explicit selected GitHub Issue intake."""

    repo: str
    issue_numbers: tuple[int, ...]
    db_path: Path | None = None
    local_repo_path: Path | None = None
    artifact_root: Path | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        repo = self.repo.strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise ValueError("repo must be in owner/name form")
        object.__setattr__(self, "repo", repo)

        normalized_numbers = _normalize_issue_numbers(self.issue_numbers)
        if not normalized_numbers:
            raise ValueError("issue_numbers must not be empty")
        object.__setattr__(self, "issue_numbers", normalized_numbers)

        db_path = self.db_path
        if db_path is None:
            from agent_taskflow.store import default_db_path

            db_path = default_db_path()
        object.__setattr__(self, "db_path", Path(db_path).expanduser())

        local_repo_path = self.local_repo_path
        if local_repo_path is None:
            local_repo_path = Path(__file__).resolve().parents[1]
        local_repo_path = require_absolute_path(local_repo_path, "local_repo_path")
        if not local_repo_path.is_dir():
            raise ValueError(f"local_repo_path must be an existing directory: {local_repo_path}")
        object.__setattr__(self, "local_repo_path", local_repo_path)

        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                require_absolute_path(self.artifact_root, "artifact_root"),
            )


IssueFetcher = Callable[[str, int], GitHubIssueSnapshot]


def fetch_selected_issue_with_gh(repo: str, issue_number: int) -> GitHubIssueSnapshot:
    """Fetch one GitHub Issue through the read-only gh issue view command."""

    from agent_taskflow.github_issue_ingestion import fetch_issue_with_gh

    try:
        return fetch_issue_with_gh(repo, issue_number)
    except Exception as exc:
        raise GitHubIssueIntakeError(str(exc)) from exc


def intake_selected_github_issues(
    request: GitHubIssueIntakeRequest,
    *,
    store: TaskMirrorStore | None = None,
    fetcher: IssueFetcher = fetch_selected_issue_with_gh,
) -> dict[str, Any]:
    """Intake only the explicitly selected GitHub Issues."""

    local_store = store or TaskMirrorStore(request.db_path)
    artifact_root = request.artifact_root or request.local_repo_path / ".agent-taskflow" / "artifacts"

    ingested: list[dict[str, Any]] = []
    already_ingested: list[dict[str, Any]] = []
    not_eligible: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    existing_matches = read_local_issue_matches(request.db_path, repo=request.repo)

    for issue_number in request.issue_numbers:
        task_key = _task_key_for_issue(issue_number)
        try:
            issue = fetcher(request.repo, issue_number)
        except Exception as exc:
            failed.append(
                {
                    "issue_number": issue_number,
                    "task_key": task_key,
                    "reason": str(exc),
                }
            )
            continue

        existing_match = existing_matches.get(issue.number)
        if existing_match is not None or _task_exists(local_store, task_key):
            already_ingested.append(
                {
                    "issue_number": issue.number,
                    "task_key": (existing_match.task_key if existing_match else task_key),
                    "reason": "matching issue already exists in local task mirror",
                }
            )
            continue

        blocked_reason = _eligibility_reason(issue)
        if blocked_reason is not None:
            not_eligible.append(
                {
                    "issue_number": issue.number,
                    "task_key": task_key,
                    "reason": blocked_reason["reason"],
                    **blocked_reason.get("details", {}),
                }
            )
            continue

        if request.dry_run:
            ingested.append(
                {
                    "issue_number": issue.number,
                    "task_key": task_key,
                    "status": "queued",
                    "artifact_kind": "issue_spec",
                    "event_type": INGESTION_EVENT_TYPE,
                    "reason": "selected open issue would be ingested into local task mirror",
                    "issue_url": issue.url,
                    "artifact_path": str(artifact_dir_for(task_key, artifact_root) / ISSUE_SPEC_FILENAME),
                }
            )
            continue

        issue_spec_path = _write_selected_issue(
            local_store,
            artifact_root=artifact_root,
            repo=request.repo,
            local_repo_path=request.local_repo_path,
            issue=issue,
            task_key=task_key,
        )
        ingested.append(
            {
                "issue_number": issue.number,
                "task_key": task_key,
                "status": "queued",
                "artifact_kind": "issue_spec",
                "event_type": INGESTION_EVENT_TYPE,
                "reason": "selected open issue ingested into local task mirror",
                "issue_url": issue.url,
                "artifact_path": str(issue_spec_path),
            }
        )

        existing_matches[issue.number] = LocalIssueMatch(
            issue_number=issue.number,
            task_key=task_key,
            title=issue.title,
        )

    summary = {
        "selected_count": len(request.issue_numbers),
        "ingested_count": len(ingested),
        "already_ingested_count": len(already_ingested),
        "not_eligible_count": len(not_eligible),
        "failed_count": len(failed),
    }

    if request.dry_run:
        status = "dry_run"
    elif summary["failed_count"] > 0 and summary["ingested_count"] == 0 and summary["already_ingested_count"] == 0 and summary["not_eligible_count"] == 0:
        status = "blocked"
    else:
        status = "completed"

    return {
        "ok": status != "blocked",
        "status": status,
        "repo": request.repo,
        "selected_issue_numbers": list(request.issue_numbers),
        "local_repo_path": str(request.local_repo_path),
        "db_path": str(request.db_path),
        "artifact_root": str(artifact_root),
        "ingested": ingested,
        "already_ingested": already_ingested,
        "not_eligible": not_eligible,
        "failed": failed,
        "summary": summary,
        "safety": _safety_block(request, ingested=ingested),
    }


def intake_result_to_dict(result: dict[str, Any]) -> dict[str, Any]:
    return result


def _write_selected_issue(
    store: TaskMirrorStore,
    *,
    artifact_root: Path,
    repo: str,
    local_repo_path: Path,
    issue: GitHubIssueSnapshot,
    task_key: str,
) -> Path:
    store.init_db()
    artifact_dir = artifact_dir_for(task_key, artifact_root)
    issue_spec_path = artifact_dir / ISSUE_SPEC_FILENAME

    existing = store.get_task(task_key)
    if existing is not None:
        raise GitHubIssueIntakeError(
            f"Task already exists for selected issue: {task_key}"
        )

    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=_project_from_repo(repo),
            board=_project_from_repo(repo),
            title=issue.title,
            status="queued",
            repo_path=local_repo_path,
            artifact_dir=artifact_dir,
        )
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    issue_spec_path.write_text(
        render_issue_spec(
            repo=repo,
            task_key=task_key,
            issue=issue,
            ingested_at=utc_now_iso(),
        ),
        encoding="utf-8",
    )
    store.record_task_artifact(task_key, "issue_spec", issue_spec_path)
    store.record_task_event(
        task_key,
        INGESTION_EVENT_TYPE,
        INGESTION_SOURCE,
        message="GitHub issue ingested",
        payload={
            "kind": INGESTION_EVENT_TYPE,
            "repo": repo,
            "issue_number": issue.number,
            "issue_url": issue.url,
            "title": issue.title,
            "labels": list(issue.labels),
            "task_key": task_key,
            "artifact_kind": "issue_spec",
            "artifact_path": str(issue_spec_path),
            "selected_intake": True,
            "dry_run": False,
        },
    )

    return issue_spec_path


def _eligibility_reason(issue: GitHubIssueSnapshot) -> dict[str, Any] | None:
    if issue.state != "open":
        return {
            "reason": "issue state is not open",
        }

    blocked_labels = sorted({label.lower() for label in issue.labels} & BLOCKED_LABELS)
    if blocked_labels:
        return {
            "reason": "issue has blocked label",
            "details": {"blocked_labels": blocked_labels},
        }

    return None


def _safety_block(
    request: GitHubIssueIntakeRequest,
    *,
    ingested: list[dict[str, Any]],
) -> dict[str, Any]:
    wrote = bool(ingested) and not request.dry_run
    return {
        "read_only": request.dry_run,
        "selected_intake_only": True,
        "db_written": wrote,
        "artifact_written": wrote,
        "event_recorded": wrote,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
    }


def _task_exists(store: TaskMirrorStore, task_key: str) -> bool:
    return False if store.db_path.exists() is False else store.get_task(task_key) is not None


def _task_key_for_issue(issue_number: int) -> str:
    return normalize_task_key(f"{DEFAULT_TASK_KEY_PREFIX}-{issue_number}")


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


def _project_from_repo(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]
