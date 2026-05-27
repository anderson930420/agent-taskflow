"""One-shot GitHub Issue discovery to one-task watcher automation.

This module is a thin outer loop over existing primitives:

discover_github_issues -> ingest_github_issue -> run_scheduler_watcher_one_task.

It processes at most one recommended GitHub Issue per invocation and stops.
It is not a daemon, scheduler loop, webhook, cron job, background worker, or
multi-task queue. Human review and merge remain external gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.github_issue_discovery import (
    BLOCKED_LABELS,
    GitHubIssueDiscoveryError,
    GitHubIssueDiscoveryRequest,
    IssueListFetcher,
    discover_github_issues,
)
from agent_taskflow.github_issue_ingestion import (
    GitHubIssueIngestionError,
    GitHubIssueIngestionRequest,
    IssueFetcher,
    ingest_github_issue,
    ingestion_result_to_dict,
)
from agent_taskflow.models import require_absolute_path
from agent_taskflow.scheduler_watcher_one_task import (
    SchedulerWatcherOneTaskError,
    SchedulerWatcherOneTaskRequest,
    run_scheduler_watcher_one_task,
)
from agent_taskflow.store import TaskMirrorStore


GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION = (
    "github_issue_one_task_automation.v1"
)
GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE = "github_issue_one_task_automation"

_FAILED_STAGE_DISCOVERY = "discovery"
_FAILED_STAGE_CONFIRMATION_FLAGS = "confirmation_flags"
_FAILED_STAGE_SELECTION = "selection"
_FAILED_STAGE_INGESTION = "ingestion"
_FAILED_STAGE_WATCHER = "watcher"

_CONFIRMATION_FLAGS: tuple[tuple[str, str], ...] = (
    ("confirm_ingest_issue", "--confirm-ingest-issue"),
    ("confirm_run_watcher_one_task", "--confirm-run-watcher-one-task"),
    ("confirm_run_one_shot_pipeline", "--confirm-run-one-shot-pipeline"),
    ("confirm_prepare_pr", "--confirm-prepare-pr"),
    ("confirm_github_mutations", "--confirm-github-mutations"),
    ("confirm_branch_push", "--confirm-branch-push"),
    ("confirm_draft_pr", "--confirm-draft-pr"),
)


class GitHubIssueOneTaskAutomationError(RuntimeError):
    """Raised when the one-shot GitHub Issue automation cannot proceed."""


@dataclass(frozen=True)
class GitHubIssueOneTaskAutomationRequest:
    """Inputs for one-shot GitHub Issue to one-task watcher automation."""

    repo: str
    db_path: Path
    local_repo_path: Path
    artifact_root: Path

    dry_run: bool = True
    issue_limit: int = 100
    include_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()

    select_first_issue: bool = False
    confirm_select_first_issue: bool = False

    confirm_ingest_issue: bool = False
    confirm_run_watcher_one_task: bool = False
    confirm_run_one_shot_pipeline: bool = False
    confirm_prepare_pr: bool = False
    confirm_github_mutations: bool = False
    confirm_branch_push: bool = False
    confirm_draft_pr: bool = False

    operator: str | None = None
    operator_note: str | None = None
    recommended_command_kind: str | None = "task_to_draft_pr"
    remote: str = "origin"
    base_branch: str | None = None
    draft: bool = True

    def __post_init__(self) -> None:
        repo = str(self.repo or "").strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise ValueError("repo must be in owner/name form")
        object.__setattr__(self, "repo", repo)

        object.__setattr__(
            self,
            "db_path",
            require_absolute_path(self.db_path, "db_path"),
        )

        local_repo_path = require_absolute_path(
            self.local_repo_path, "local_repo_path"
        )
        if not local_repo_path.is_dir():
            raise ValueError(
                f"local_repo_path must be an existing directory: {local_repo_path}"
            )
        object.__setattr__(self, "local_repo_path", local_repo_path)

        object.__setattr__(
            self,
            "artifact_root",
            require_absolute_path(self.artifact_root, "artifact_root"),
        )

        if self.issue_limit <= 0:
            raise ValueError("issue_limit must be positive")

        object.__setattr__(
            self,
            "include_labels",
            _normalize_labels(self.include_labels),
        )
        object.__setattr__(
            self,
            "exclude_labels",
            _normalize_labels(self.exclude_labels),
        )

        for field_name in (
            "operator",
            "operator_note",
            "recommended_command_kind",
            "base_branch",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = str(value).strip()
            object.__setattr__(self, field_name, stripped or None)

        remote = str(self.remote or "").strip()
        if not remote:
            raise ValueError("remote must not be empty")
        object.__setattr__(self, "remote", remote)


def run_github_issue_one_task_automation(
    request: GitHubIssueOneTaskAutomationRequest,
    *,
    discovery_fetcher: IssueListFetcher | None = None,
    ingestion_fetcher: IssueFetcher | None = None,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None = None,
    branch_push_fn: Callable[..., dict[str, Any]] | None = None,
    draft_pr_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Discover, ingest, and run the watcher for at most one GitHub Issue."""

    if not request.draft:
        raise GitHubIssueOneTaskAutomationError(
            "GitHub Issue one-task automation supports draft PR creation only"
        )

    try:
        discovery = _run_discovery(request, discovery_fetcher=discovery_fetcher)
    except (GitHubIssueDiscoveryError, ValueError) as exc:
        return _failure_response(
            request,
            status="discovery_failed",
            failed_stage=_FAILED_STAGE_DISCOVERY,
            reasons=[str(exc)],
            discovery=None,
            selected_issue=None,
            ingestion=None,
            watcher=None,
        )

    recommended_candidates = list(discovery.get("recommended_candidates") or [])

    if request.dry_run:
        selection = _select_first_issue(request, recommended_candidates)
        return _success_response(
            request,
            status="dry_run",
            discovery=discovery,
            selected_issue=selection.get("issue") if selection["ok"] else None,
            ingestion=None,
            watcher=None,
            selected_task_key=None,
            safety=_safety(dry_run=True, discovery_called=True),
            selection={
                "would_select_issue": bool(selection["ok"]),
                "reason": selection.get("reason"),
                "candidate_count": len(recommended_candidates),
            },
        )

    missing = _missing_confirmations(request)
    if missing:
        return _failure_response(
            request,
            status="confirmation_required",
            failed_stage=_FAILED_STAGE_CONFIRMATION_FLAGS,
            reasons=[
                "confirmed GitHub Issue one-task automation requires: "
                + ", ".join(missing)
            ],
            discovery=discovery,
            selected_issue=None,
            ingestion=None,
            watcher=None,
        )

    selection = _select_first_issue(request, recommended_candidates)
    if not selection["ok"]:
        if selection.get("reason") == "no_eligible_issues":
            return _success_response(
                request,
                status="no_eligible_issues",
                discovery=discovery,
                selected_issue=None,
                ingestion=None,
                watcher=None,
                selected_task_key=None,
                safety=_safety(dry_run=False, discovery_called=True),
                selection={
                    "would_select_issue": False,
                    "reason": "no_eligible_issues",
                    "candidate_count": len(recommended_candidates),
                },
            )
        return _failure_response(
            request,
            status="selection_blocked",
            failed_stage=_FAILED_STAGE_SELECTION,
            reasons=[str(selection.get("reason") or "selection_failed")],
            discovery=discovery,
            selected_issue=selection.get("issue"),
            ingestion=None,
            watcher=None,
        )

    selected_issue = selection["issue"]
    ingestion_request = GitHubIssueIngestionRequest(
        repo=request.repo,
        issue_number=int(selected_issue["number"]),
        local_repo_path=request.local_repo_path,
        artifact_root=request.artifact_root,
        dry_run=False,
    )
    try:
        if ingestion_fetcher is None:
            ingestion_result = ingest_github_issue(
                ingestion_request,
                store=TaskMirrorStore(request.db_path),
            )
        else:
            ingestion_result = ingest_github_issue(
                ingestion_request,
                store=TaskMirrorStore(request.db_path),
                fetcher=ingestion_fetcher,
            )
    except (GitHubIssueIngestionError, ValueError) as exc:
        return _failure_response(
            request,
            status="ingestion_failed",
            failed_stage=_FAILED_STAGE_INGESTION,
            reasons=[str(exc)],
            discovery=discovery,
            selected_issue=selected_issue,
            ingestion=None,
            watcher=None,
        )

    ingestion = ingestion_result_to_dict(ingestion_result)
    selected_task_key = ingestion_result.task_key

    if ingestion_result.ok is not True:
        return _failure_response(
            request,
            status="ingestion_failed",
            failed_stage=_FAILED_STAGE_INGESTION,
            reasons=[ingestion_result.summary or "ingestion_not_ok"],
            discovery=discovery,
            selected_issue=selected_issue,
            ingestion=ingestion,
            watcher=None,
            issue_ingested=_ingestion_wrote(ingestion),
            selected_task_key=selected_task_key,
        )

    try:
        watcher = run_scheduler_watcher_one_task(
            SchedulerWatcherOneTaskRequest(
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                dry_run=False,
                confirm_run_watcher_one_task=True,
                task_key=selected_task_key,
                resume_existing=True,
                resume_pr_preparation=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
                operator=request.operator,
                operator_note=request.operator_note,
                recommended_command_kind=_watcher_recommended_command_kind(
                    request.recommended_command_kind
                ),
                remote=request.remote,
                base_branch=request.base_branch,
                draft=True,
            ),
            approved_task_runner_fn=approved_task_runner_fn,
            branch_push_fn=branch_push_fn,
            draft_pr_fn=draft_pr_fn,
        )
    except (SchedulerWatcherOneTaskError, ValueError) as exc:
        return _failure_response(
            request,
            status="watcher_failed",
            failed_stage=_FAILED_STAGE_WATCHER,
            reasons=[str(exc)],
            discovery=discovery,
            selected_issue=selected_issue,
            ingestion=ingestion,
            watcher=None,
            issue_ingested=_ingestion_wrote(ingestion),
            watcher_called=True,
            selected_task_key=selected_task_key,
        )

    watcher_ok = watcher.get("ok") is True
    watcher_safety = watcher.get("safety") or {}
    safety = _safety(
        dry_run=False,
        discovery_called=True,
        issue_ingested=_ingestion_wrote(ingestion),
        watcher_called=True,
        approved_task_runner_called=bool(
            watcher_safety.get("approved_task_runner_called")
        ),
        github_mutated=bool(watcher_safety.get("github_mutated")),
        branch_pushed=bool(watcher_safety.get("branch_pushed")),
        draft_pr_created=bool(watcher_safety.get("draft_pr_created")),
    )

    if not watcher_ok:
        return _failure_response(
            request,
            status="watcher_failed",
            failed_stage=_FAILED_STAGE_WATCHER,
            reasons=list(watcher.get("reasons") or ["watcher_not_ok"]),
            discovery=discovery,
            selected_issue=selected_issue,
            ingestion=ingestion,
            watcher=watcher,
            issue_ingested=_ingestion_wrote(ingestion),
            watcher_called=True,
            selected_task_key=selected_task_key,
            safety=safety,
        )

    return _success_response(
        request,
        status="completed_one_task",
        discovery=discovery,
        selected_issue=selected_issue,
        ingestion=ingestion,
        watcher=watcher,
        selected_task_key=selected_task_key,
        safety=safety,
        selection={
            "would_select_issue": True,
            "reason": selection.get("reason"),
            "candidate_count": len(recommended_candidates),
        },
    )


def _run_discovery(
    request: GitHubIssueOneTaskAutomationRequest,
    *,
    discovery_fetcher: IssueListFetcher | None,
) -> dict[str, Any]:
    discovery_request = GitHubIssueDiscoveryRequest(
        repo=request.repo,
        db_path=request.db_path,
        limit=request.issue_limit,
        include_labels=request.include_labels,
        exclude_labels=request.exclude_labels,
    )
    if discovery_fetcher is None:
        return discover_github_issues(discovery_request)
    return discover_github_issues(discovery_request, fetcher=discovery_fetcher)


def _missing_confirmations(
    request: GitHubIssueOneTaskAutomationRequest,
) -> list[str]:
    missing: list[str] = []
    if not request.select_first_issue:
        missing.append("--select-first-issue")
    if not request.confirm_select_first_issue:
        missing.append("--confirm-select-first-issue")
    for field_name, flag in _CONFIRMATION_FLAGS:
        if not bool(getattr(request, field_name)):
            missing.append(flag)
    return missing


def _select_first_issue(
    request: GitHubIssueOneTaskAutomationRequest,
    recommended_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not request.select_first_issue:
        return {"ok": False, "reason": "select_first_issue_required", "issue": None}
    if not request.confirm_select_first_issue:
        return {
            "ok": False,
            "reason": "first_issue_selection_not_confirmed",
            "issue": None,
        }
    if not recommended_candidates:
        return {"ok": False, "reason": "no_eligible_issues", "issue": None}

    selected = recommended_candidates[0]
    blocked_reason = _selected_issue_block_reason(request, selected)
    if blocked_reason is not None:
        return {"ok": False, "reason": blocked_reason, "issue": selected}
    return {
        "ok": True,
        "reason": "selected_first_recommended_issue",
        "issue": selected,
    }


def _selected_issue_block_reason(
    request: GitHubIssueOneTaskAutomationRequest,
    issue: dict[str, Any],
) -> str | None:
    state = str(issue.get("state") or "").strip().lower()
    if state != "open":
        return "selected_issue_state_not_open"

    labels = {
        _normalize_label(str(label))
        for label in issue.get("labels") or []
        if str(label).strip()
    }
    if labels & BLOCKED_LABELS:
        return "selected_issue_has_blocked_label"

    missing_include = [label for label in request.include_labels if label not in labels]
    if missing_include:
        return "selected_issue_missing_include_label"

    excluded = [label for label in request.exclude_labels if label in labels]
    if excluded:
        return "selected_issue_has_excluded_label"

    try:
        number = int(issue.get("number"))
    except (TypeError, ValueError):
        return "selected_issue_number_invalid"
    if number <= 0:
        return "selected_issue_number_invalid"
    return None


def _success_response(
    request: GitHubIssueOneTaskAutomationRequest,
    *,
    status: str,
    discovery: dict[str, Any],
    selected_issue: dict[str, Any] | None,
    ingestion: dict[str, Any] | None,
    watcher: dict[str, Any] | None,
    selected_task_key: str | None,
    safety: dict[str, Any],
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _base_response(
        request,
        ok=True,
        status=status,
        discovery=discovery,
        selected_issue=selected_issue,
        ingestion=ingestion,
        watcher=watcher,
        selected_task_key=selected_task_key,
        safety=safety,
    )
    if selection is not None:
        payload["selection"] = selection
    return payload


def _failure_response(
    request: GitHubIssueOneTaskAutomationRequest,
    *,
    status: str,
    failed_stage: str,
    reasons: list[str],
    discovery: dict[str, Any] | None,
    selected_issue: dict[str, Any] | None,
    ingestion: dict[str, Any] | None,
    watcher: dict[str, Any] | None,
    issue_ingested: bool = False,
    watcher_called: bool = False,
    selected_task_key: str | None = None,
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _base_response(
        request,
        ok=False,
        status=status,
        discovery=discovery,
        selected_issue=selected_issue,
        ingestion=ingestion,
        watcher=watcher,
        selected_task_key=selected_task_key,
        safety=safety
        or _safety(
            dry_run=request.dry_run,
            discovery_called=discovery is not None,
            issue_ingested=issue_ingested,
            watcher_called=watcher_called,
        ),
    )
    payload["failed_stage"] = failed_stage
    payload["reasons"] = _unique_strings([reason for reason in reasons if reason])
    return payload


def _base_response(
    request: GitHubIssueOneTaskAutomationRequest,
    *,
    ok: bool,
    status: str,
    discovery: dict[str, Any] | None,
    selected_issue: dict[str, Any] | None,
    ingestion: dict[str, Any] | None,
    watcher: dict[str, Any] | None,
    selected_task_key: str | None,
    safety: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": ok,
        "schema_version": GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION,
        "source": GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE,
        "status": status,
        "mode": "dry_run" if request.dry_run else "confirmed",
        "repo": request.repo,
        "discovery": discovery,
        "selected_issue": selected_issue,
        "ingestion": ingestion,
        "watcher": watcher,
        "selected_task_key": selected_task_key,
        "safety": safety,
    }


def _safety(
    *,
    dry_run: bool,
    discovery_called: bool,
    issue_ingested: bool = False,
    watcher_called: bool = False,
    approved_task_runner_called: bool = False,
    github_mutated: bool = False,
    branch_pushed: bool = False,
    draft_pr_created: bool = False,
) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "one_issue_only": True,
        "one_task_only": True,
        "discovery_called": discovery_called,
        "issue_ingested": issue_ingested,
        "watcher_called": watcher_called,
        "approved_task_runner_called": approved_task_runner_called,
        "github_mutated": github_mutated,
        "branch_pushed": branch_pushed,
        "draft_pr_created": draft_pr_created,
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "scheduler_loop_started": False,
        "background_worker_started": False,
        "multi_task_batch_started": False,
        "human_review_required": True,
    }


def _ingestion_wrote(ingestion: dict[str, Any] | None) -> bool:
    if not ingestion:
        return False
    return bool(
        ingestion.get("wrote_task")
        or ingestion.get("wrote_artifact")
        or ingestion.get("recorded_event")
    )


def _watcher_recommended_command_kind(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized == "task_to_draft_pr":
        return None
    return normalized


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


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION",
    "GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE",
    "GitHubIssueOneTaskAutomationError",
    "GitHubIssueOneTaskAutomationRequest",
    "run_github_issue_one_task_automation",
]
