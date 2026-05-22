"""Waiting-approval PR handoff package generation.

This module builds on the read-only waiting-approval review summary and adds
local git inspection plus operator-facing PR handoff packaging. It does not
push branches, create pull requests, merge, approve, clean up, delete
branches, delete worktrees, or run executors/validators.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from agent_taskflow._helpers import dedupe_preserve_order as _dedupe_preserve_order
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.waiting_approval_summary import (
    WaitingApprovalSummaryRequest,
    summarize_waiting_approval_task,
)
from agent_taskflow.worktree import ensure_absolute_path


SCHEMA_VERSION = "1"
ARTIFACT_TYPE = "pr_handoff_package"
EVENT_TYPE = "pr_handoff_package_created"
SOURCE = "pr_handoff_package"
DEFAULT_DB_PATH = Path.home() / ".agent-taskflow" / "state.db"
DEFAULT_ARTIFACT_ROOT_NAME = "pr_handoff_package"
DEFAULT_REMOTE = "origin"


class PrHandoffPackageError(RuntimeError):
    """Raised when a PR handoff package cannot be generated safely."""


@dataclass(frozen=True)
class PrHandoffPackageRequest:
    """Request for creating a local PR handoff package."""

    task_key: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    dry_run: bool = False
    allow_non_waiting: bool = False
    remote: str = DEFAULT_REMOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "repo_path",
            ensure_absolute_path(self.repo_path, name="repo_path"),
        )
        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                ensure_absolute_path(self.db_path, name="db_path"),
            )
        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                ensure_absolute_path(self.artifact_root, name="artifact_root"),
            )
        normalized_remote = self.remote.strip()
        if not normalized_remote:
            raise ValueError("remote must not be empty")
        if normalized_remote.startswith("-") or any(ch.isspace() for ch in normalized_remote):
            raise ValueError("remote must be a simple git remote name")
        object.__setattr__(self, "remote", normalized_remote)


@dataclass(frozen=True)
class PrHandoffPackageResult:
    """Structured local PR handoff package result."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    review_summary: dict[str, Any]
    source: dict[str, Any]
    workspace: dict[str, Any]
    git: dict[str, Any]
    executor: dict[str, Any]
    validation: dict[str, Any]
    evidence: dict[str, Any]
    handoff: dict[str, Any]
    dry_run: dict[str, Any]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    summary: dict[str, Any]
    safety: dict[str, Any]
    warnings: list[str]
    artifact_recorded: bool
    event_recorded: bool
    package_json_path: str | None
    package_markdown_path: str | None
    generated_at: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), sort_keys=True))

    def to_markdown(self) -> str:
        lines = [
            "# PR Handoff Package",
            "",
            f"- Task key: {self.task_key}",
            f"- Task status: {self.task_status or '(unknown)'}",
            f"- Result status: {self.status}",
            f"- Ready for branch push review: {self.summary['ready_for_branch_push_review']}",
            f"- Ready for draft PR review: {self.summary['ready_for_draft_pr_review']}",
            "",
            "## Source",
            f"- Available: {self.source.get('available')}",
            f"- Issue URL: {self.source.get('issue_url') or '(none)'}",
            f"- Title: {self.source.get('title') or '(none)'}",
            f"- Labels: {', '.join(self.source.get('labels', [])) if self.source.get('labels') else '(none)'}",
            "",
            "## Workspace",
            f"- Worktree path: {self.workspace.get('worktree_path') or '(none)'}",
            f"- Branch: {self.workspace.get('branch') or '(none)'}",
            f"- Base branch: {self.workspace.get('base_branch') or '(none)'}",
            f"- Base SHA: {self.workspace.get('base_sha') or '(none)'}",
            "",
            "## Git",
            f"- Changed files: {self.git.get('changed_file_count', 0)}",
            f"- Worktree clean: {self.git.get('worktree_clean')}",
            f"- Diff summary: {self.git.get('diff_summary') or '(none)'}",
            "",
            "## Validation",
            f"- Executor finished ok: {self.executor.get('finished_ok')}",
            f"- Validators passed: {self.validation.get('all_passed')}",
            "",
            "## Proposed PR",
            f"- Title: {self.handoff['proposed_pr_title']}",
            f"- Base: {self.handoff['proposed_pr_base']}",
            f"- Head: {self.handoff['proposed_pr_head']}",
            "",
            "```text",
            self.handoff["proposed_pr_body"],
            "```",
            "",
            "## Dry Run",
            f"- Branch push preview: {self.dry_run['branch_push']['command_preview']}",
            f"- Draft PR preview: {self.dry_run['draft_pr']['command_preview']}",
            "",
            "## Review Readiness",
            f"- Blocking warnings: {len(self.review_summary['blocking_warnings'])}",
        ]
        if self.review_summary["blocking_warnings"]:
            lines.append("")
            lines.append("Blocking warnings:")
            lines.extend(f"- {warning}" for warning in self.review_summary["blocking_warnings"])
        if self.warnings:
            lines.append("")
            lines.append("Package warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings)
        lines.append("")
        lines.append("This package does not push branches, create pull requests, merge, approve, or clean up.")
        return "\n".join(lines) + "\n"


def create_pr_handoff_package(
    request: PrHandoffPackageRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> PrHandoffPackageResult:
    """Create a deterministic local PR handoff package."""

    summary_request = WaitingApprovalSummaryRequest(
        task_key=request.task_key,
        db_path=request.db_path,
        allow_non_waiting=request.allow_non_waiting,
    )
    review_summary = summarize_waiting_approval_task(summary_request)
    if not review_summary.ok:
        blocking_warnings = review_summary.review_readiness.get("blocking_warnings", [])
        inferred_error = review_summary.error
        if not inferred_error and blocking_warnings:
            inferred_error = blocking_warnings[0]
        return _error_result(
            request.task_key,
            status=review_summary.status,
            error=inferred_error or review_summary.summary["next_phase"],
        )

    task = review_summary.task
    if task.get("status") != "waiting_approval" and not request.allow_non_waiting:
        return _error_result(
            request.task_key,
            status="blocked",
            error=f"Task {request.task_key} must be waiting_approval, got {task.get('status')}",
        )

    warnings = [
        warning
        for warning in review_summary.warnings
        if warning != "No approval/review evidence is present yet"
    ]
    task_repo_path = task.get("repo_path")
    if task_repo_path and Path(task_repo_path).resolve() != request.repo_path.resolve():
        warnings.append(
            f"Provided repo_path {request.repo_path} does not match task repo_path {task_repo_path}"
        )

    git_result = _inspect_git_state(
        request.repo_path,
        review_summary.workspace,
        warnings=warnings,
        remote=request.remote,
        task_title=task.get("title"),
        task_key=request.task_key,
    )

    blocking_warnings = _blocking_warnings(
        review_summary=review_summary.review_readiness,
        git_result=git_result,
        repo_path=request.repo_path,
        task_repo_path=task_repo_path,
    )
    warnings = _dedupe_preserve_order(warnings + blocking_warnings)

    ready_for_branch_push_review = bool(
        git_result["available"]
        and review_summary.source["available"]
        and review_summary.workspace["available"]
        and review_summary.executor["available"]
        and review_summary.executor["finished_ok"]
        and review_summary.validators["available"]
        and review_summary.validators["all_passed"]
        and not blocking_warnings
    )
    ready_for_draft_pr_review = ready_for_branch_push_review

    base_branch = review_summary.workspace.get("base_branch") or "main"
    branch = review_summary.workspace.get("branch") or ""
    head_sha = git_result.get("head_sha")
    proposed_pr_title = _proposed_pr_title(task_key=request.task_key, task_title=task.get("title"))
    proposed_pr_body = _proposed_pr_body(
        task_key=request.task_key,
        task=task,
        source=review_summary.source,
        workspace=review_summary.workspace,
        git_result=git_result,
        review_summary=review_summary.review_readiness,
        validation=review_summary.validators,
        executor=review_summary.executor,
    )

    output_paths = _package_output_paths(
        request=request,
        task=task,
    )
    generated_at = utc_now_iso()
    package_artifacts = [
        {
            "kind": ARTIFACT_TYPE,
            "artifact_type": ARTIFACT_TYPE,
            "name": "pr_handoff_package.json",
            "path": str(output_paths["json_path"]) if output_paths["json_path"] else None,
            "available": bool(output_paths["json_path"] and output_paths["json_path"].is_file()),
            "source": "local",
        },
        {
            "kind": ARTIFACT_TYPE,
            "artifact_type": ARTIFACT_TYPE,
            "name": "pr_handoff_package.md",
            "path": str(output_paths["markdown_path"]) if output_paths["markdown_path"] else None,
            "available": bool(output_paths["markdown_path"] and output_paths["markdown_path"].is_file()),
            "source": "local",
        },
    ]

    package_data = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": request.task_key,
        "task_status": task.get("status"),
        "review_summary": review_summary.review_readiness,
        "source": review_summary.source,
        "workspace": {
            **review_summary.workspace,
            "repo_path": str(request.repo_path),
        },
        "git": {
            **git_result,
            "base_branch": base_branch,
            "base_sha": review_summary.workspace.get("base_sha"),
            "branch": branch,
            "repo_path": str(request.repo_path),
            "head_sha": head_sha,
        },
        "executor": review_summary.executor,
        "validation": review_summary.validators,
        "evidence": review_summary.evidence,
        "handoff": {
            "proposed_pr_title": proposed_pr_title,
            "proposed_pr_body": proposed_pr_body,
            "proposed_pr_base": base_branch,
            "proposed_pr_head": branch,
            "package_artifacts": package_artifacts,
            "evidence_artifacts": _evidence_artifacts(review_summary),
        },
        "dry_run": {
            "branch_push": _branch_push_preview(
                branch=branch,
                remote=request.remote,
                base_branch=base_branch,
                base_sha=review_summary.workspace.get("base_sha"),
            ),
            "draft_pr": _draft_pr_preview(
                title=proposed_pr_title,
                body=proposed_pr_body,
                base_branch=base_branch,
                head_branch=branch,
            ),
        },
        "next_allowed_actions": _next_allowed_actions(ready_for_branch_push_review=ready_for_branch_push_review),
        "actions_not_performed": [
            "branch push",
            "PR creation",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        "summary": {
            "handoff_package_created": True,
            "ready_for_branch_push_review": ready_for_branch_push_review,
            "ready_for_draft_pr_review": ready_for_draft_pr_review,
            "requires_human_confirmation": True,
            "next_phase": "explicit_branch_push_confirm"
            if ready_for_branch_push_review
            else "evidence_remediation",
        },
        "safety": _safety_block(
            dry_run=request.dry_run,
            local_artifact_written=False,
            local_event_recorded=False,
        ),
        "warnings": warnings,
        "generated_at": generated_at,
        "artifact_recorded": False,
        "event_recorded": False,
        "package_json_path": str(output_paths["json_path"]) if output_paths["json_path"] else None,
        "package_markdown_path": str(output_paths["markdown_path"]) if output_paths["markdown_path"] else None,
    }

    artifact_recorded = False
    event_recorded = False
    if (
        not request.dry_run
        and output_paths["json_path"] is not None
        and output_paths["markdown_path"] is not None
    ):
        artifact_recorded, event_recorded = _write_local_evidence(
            request=request,
            package_data=package_data,
            output_paths=output_paths,
            store=store,
        )
        package_data["safety"] = _safety_block(
            dry_run=False,
            local_artifact_written=artifact_recorded,
            local_event_recorded=event_recorded,
        )
        package_data["artifact_recorded"] = artifact_recorded
        package_data["event_recorded"] = event_recorded
        package_data["handoff"]["package_artifacts"] = [
            {
                **package_artifact,
                "available": bool(package_artifact["path"])
                and Path(str(package_artifact["path"])).is_file(),
            }
            for package_artifact in package_data["handoff"]["package_artifacts"]
        ]

    return PrHandoffPackageResult(
        ok=True,
        status="ok",
        task_key=request.task_key,
        task_status=task.get("status"),
        review_summary=review_summary.review_readiness,
        source=review_summary.source,
        workspace=package_data["workspace"],
        git=package_data["git"],
        executor=review_summary.executor,
        validation=review_summary.validators,
        evidence=review_summary.evidence,
        handoff=package_data["handoff"],
        dry_run=package_data["dry_run"],
        next_allowed_actions=package_data["next_allowed_actions"],
        actions_not_performed=package_data["actions_not_performed"],
        summary=package_data["summary"],
        safety=package_data["safety"],
        warnings=warnings,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        package_json_path=package_data["package_json_path"],
        package_markdown_path=package_data["package_markdown_path"],
        generated_at=generated_at,
    )


def _package_output_paths(
    *,
    request: PrHandoffPackageRequest,
    task: dict[str, Any],
) -> dict[str, Path | None]:
    artifact_dir = task.get("artifact_dir")
    if request.artifact_root is not None:
        output_root = request.artifact_root / DEFAULT_ARTIFACT_ROOT_NAME
    elif artifact_dir:
        output_root = Path(artifact_dir).expanduser().resolve().parent / DEFAULT_ARTIFACT_ROOT_NAME
    else:
        return {
            "output_root": None,
            "json_path": None,
            "markdown_path": None,
        }

    task_dir = output_root / request.task_key
    return {
        "output_root": output_root,
        "json_path": task_dir / "pr_handoff_package.json",
        "markdown_path": task_dir / "pr_handoff_package.md",
    }


def _inspect_git_state(
    repo_path: Path,
    workspace: dict[str, Any],
    *,
    warnings: list[str],
    remote: str,
    task_title: str | None,
    task_key: str,
) -> dict[str, Any]:
    worktree_path = workspace.get("worktree_path")
    if not worktree_path:
        warnings.append("Worktree path is unavailable; git inspection was skipped")
        return _unavailable_git_state(warnings=warnings)

    worktree = Path(str(worktree_path))
    if not worktree.exists():
        warnings.append(f"Worktree path is missing on disk: {worktree}")
        return _unavailable_git_state(warnings=warnings)

    try:
        head_sha = _git(worktree, ["rev-parse", "HEAD"])
        current_branch = _git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
        status_short = _git(worktree, ["status", "--porcelain=v1", "--untracked-files=all"])
        log_output = _git(worktree, ["log", "--oneline", "-n", "5"])
    except PrHandoffPackageError as exc:
        warnings.append(str(exc))
        return _unavailable_git_state(warnings=warnings)

    base_sha = str(workspace.get("base_sha") or "").strip()
    if not base_sha:
        warnings.append("Base SHA is unavailable; committed diff inspection was skipped")
        return _unavailable_git_state(warnings=warnings)

    try:
        diff_names = _git(worktree, ["diff", "--name-only", f"{base_sha}..HEAD"])
        diff_stat = _git(worktree, ["diff", "--stat", f"{base_sha}..HEAD"])
    except PrHandoffPackageError as exc:
        warnings.append(str(exc))
        return _unavailable_git_state(warnings=warnings)

    changed_files = _changed_files(diff_names=diff_names)
    commit_summary = _commit_summary(log_output)
    command_preview = _branch_push_preview_text(
        branch=str(workspace.get("branch") or current_branch),
        remote=remote,
        dry_run=True,
    )
    return {
        "available": True,
        "repo_path": str(repo_path),
        "head_sha": head_sha,
        "current_branch": current_branch,
        "status_short": status_short.splitlines(),
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "diff_summary": diff_stat if diff_stat else "(clean)",
        "commit_summary": commit_summary,
        "worktree_clean": not status_short.strip(),
        "branch_push_command_preview": command_preview,
        "warnings": [],
    }


def _unavailable_git_state(*, warnings: list[str]) -> dict[str, Any]:
    return {
        "available": False,
        "repo_path": None,
        "head_sha": None,
        "current_branch": None,
        "status_short": [],
        "changed_files": [],
        "changed_file_count": 0,
        "diff_summary": None,
        "commit_summary": [],
        "worktree_clean": False,
        "branch_push_command_preview": None,
        "draft_pr_command_preview": None,
        "warnings": list(warnings),
    }


def _changed_files(*, diff_names: str) -> list[str]:
    names: set[str] = set()
    for raw_line in diff_names.splitlines():
        name = raw_line.strip()
        if name:
            names.add(name)
    return sorted(names)


def _commit_summary(log_output: str) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for raw_line in log_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if " " in line:
            sha, subject = line.split(" ", 1)
        else:
            sha, subject = line, ""
        summary.append({"sha": sha, "subject": subject})
    return summary


def _proposed_pr_title(*, task_key: str, task_title: str | None) -> str:
    title = (task_title or "PR handoff").strip()
    return f"{task_key}: {title}"


def _proposed_pr_body(
    *,
    task_key: str,
    task: dict[str, Any],
    source: dict[str, Any],
    workspace: dict[str, Any],
    git_result: dict[str, Any],
    review_summary: dict[str, Any],
    validation: dict[str, Any],
    executor: dict[str, Any],
) -> str:
    lines = [
        f"Task: {task_key}",
        f"Task status: {task.get('status')}",
        f"Task title: {task.get('title') or '(none)'}",
        "",
        "Source issue/spec:",
        f"- Repo: {source.get('repo') or '(none)'}",
        f"- Issue number: {source.get('issue_number') or '(none)'}",
        f"- Issue URL: {source.get('issue_url') or '(none)'}",
        f"- Issue title: {source.get('title') or '(none)'}",
        "",
        "Workspace:",
        f"- Worktree path: {workspace.get('worktree_path') or '(none)'}",
        f"- Branch: {workspace.get('branch') or '(none)'}",
        f"- Base branch: {workspace.get('base_branch') or '(none)'}",
        f"- Base SHA: {workspace.get('base_sha') or '(none)'}",
        "",
        "Git summary:",
        f"- Head SHA: {git_result.get('head_sha') or '(none)'}",
        f"- Changed files: {', '.join(git_result.get('changed_files', [])) if git_result.get('changed_files') else '(none)'}",
        f"- Diff summary: {git_result.get('diff_summary') or '(none)'}",
        "",
        "Executor:",
        f"- Executor: {executor.get('executor') or '(none)'}",
        f"- Finished ok: {executor.get('finished_ok')}",
        f"- Summary: {executor.get('summary') or '(none)'}",
        "",
        "Validators:",
    ]
    if validation.get("results"):
        lines.extend(
            f"- {item.get('validator')}: {item.get('status')} ({item.get('summary') or ''})"
            for item in validation["results"]
        )
    else:
        lines.append("- No validator results recorded.")
    lines.extend(
        [
            "",
            "Review readiness:",
            f"- Ready for human review: {review_summary['ready_for_human_review']}",
        ]
    )
    if review_summary["blocking_warnings"]:
        lines.append("- Blocking warnings:")
        lines.extend(f"  - {warning}" for warning in review_summary["blocking_warnings"])
    lines.extend(
        [
            "",
            "Governance:",
            "- no auto-merge",
            "- human review required",
            "",
            "This package is a local handoff package only. It does not push branches, create pull requests, merge, approve, or clean up.",
        ]
    )
    return "\n".join(lines)


def _branch_push_preview(
    *,
    branch: str,
    remote: str,
    base_branch: str,
    base_sha: str | None,
) -> dict[str, Any]:
    command = ["git", "push", "--dry-run", "--set-upstream", remote, branch]
    return {
        "would_push": bool(branch),
        "remote": remote,
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "command_preview": " ".join(shlex.quote(part) for part in command),
        "requires_explicit_confirmation": True,
        "performed": False,
    }


def _draft_pr_preview(
    *,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
) -> dict[str, Any]:
    command = [
        "gh",
        "pr",
        "create",
        "--draft",
        "--base",
        base_branch,
        "--head",
        head_branch,
        "--title",
        title,
        "--body",
        body,
    ]
    return {
        "would_create_pr": True,
        "base": base_branch,
        "head": head_branch,
        "title": title,
        "body_preview": body,
        "command_preview": " ".join(shlex.quote(part) for part in command),
        "requires_explicit_confirmation": True,
        "performed": False,
    }


def _build_markdown(package_data: dict[str, Any]) -> str:
    lines = [
        "# PR Handoff Package",
        "",
        f"- Task key: {package_data['task_key']}",
        f"- Task status: {package_data.get('task_status') or '(unknown)'}",
        f"- Result status: {package_data['summary']['next_phase'] if not package_data['summary']['handoff_package_created'] else 'ok'}",
        f"- Ready for branch push review: {package_data['summary']['ready_for_branch_push_review']}",
        f"- Ready for draft PR review: {package_data['summary']['ready_for_draft_pr_review']}",
        "",
        "## Source",
        f"- Available: {package_data['source'].get('available')}",
        f"- Issue URL: {package_data['source'].get('issue_url') or '(none)'}",
        f"- Title: {package_data['source'].get('title') or '(none)'}",
        f"- Labels: {', '.join(package_data['source'].get('labels', [])) if package_data['source'].get('labels') else '(none)'}",
        "",
        "## Workspace",
        f"- Worktree path: {package_data['workspace'].get('worktree_path') or '(none)'}",
        f"- Branch: {package_data['workspace'].get('branch') or '(none)'}",
        f"- Base branch: {package_data['workspace'].get('base_branch') or '(none)'}",
        f"- Base SHA: {package_data['workspace'].get('base_sha') or '(none)'}",
        "",
        "## Git",
        f"- Changed files: {package_data['git'].get('changed_file_count', 0)}",
        f"- Worktree clean: {package_data['git'].get('worktree_clean')}",
        f"- Diff summary: {package_data['git'].get('diff_summary') or '(none)'}",
        "",
        "## Validation",
        f"- Executor finished ok: {package_data['executor'].get('finished_ok')}",
        f"- Validators passed: {package_data['validation'].get('all_passed')}",
        "",
        "## Proposed PR",
        f"- Title: {package_data['handoff']['proposed_pr_title']}",
        f"- Base: {package_data['handoff']['proposed_pr_base']}",
        f"- Head: {package_data['handoff']['proposed_pr_head']}",
        "",
        "```text",
        package_data["handoff"]["proposed_pr_body"],
        "```",
        "",
        "## Dry Run",
        f"- Branch push preview: {package_data['dry_run']['branch_push']['command_preview']}",
        f"- Draft PR preview: {package_data['dry_run']['draft_pr']['command_preview']}",
        "",
        "## Review Readiness",
        f"- Blocking warnings: {len(package_data['review_summary']['blocking_warnings'])}",
    ]
    if package_data["review_summary"]["blocking_warnings"]:
        lines.append("")
        lines.append("Blocking warnings:")
        lines.extend(f"- {warning}" for warning in package_data["review_summary"]["blocking_warnings"])
    if package_data.get("warnings"):
        lines.append("")
        lines.append("Package warnings:")
        lines.extend(f"- {warning}" for warning in package_data["warnings"])
    lines.append("")
    lines.append("This package does not push branches, create pull requests, merge, approve, or clean up.")
    return "\n".join(lines) + "\n"


def _branch_push_preview_text(*, branch: str, remote: str, dry_run: bool) -> str:
    command = ["git", "push"]
    if dry_run:
        command.append("--dry-run")
    command.extend(["--set-upstream", remote, branch])
    return " ".join(shlex.quote(part) for part in command)


def _next_allowed_actions(*, ready_for_branch_push_review: bool) -> list[str]:
    if not ready_for_branch_push_review:
        return [
            "manual review of handoff package",
            "resolve blocking warnings",
            "rerun the waiting-approval handoff package command after evidence is complete",
        ]
    return [
        "manual review of handoff package",
        "explicit branch push dry-run review",
        "explicit branch push confirm in a later phase",
        "explicit draft PR dry-run review",
        "explicit draft PR creation confirm in a later phase",
    ]


def _blocking_warnings(
    *,
    review_summary: dict[str, Any],
    git_result: dict[str, Any],
    repo_path: Path,
    task_repo_path: str | None,
) -> list[str]:
    warnings: list[str] = []
    warnings.extend(
        warning
        for warning in review_summary["blocking_warnings"]
        if warning != "No approval/review evidence is present yet"
    )
    if task_repo_path and Path(task_repo_path).resolve() != repo_path.resolve():
        warnings.append("Provided repo_path does not match the task repo_path")
    if not git_result["available"]:
        warnings.append("Git state could not be inspected")
    elif not git_result["changed_files"] and not git_result["worktree_clean"]:
        warnings.append("Changed files could not be determined deterministically")
    return warnings


def _evidence_artifacts(review_summary: Any) -> list[dict[str, Any]]:
    artifacts = []
    for item in review_summary.artifacts:
        artifacts.append(
            {
                "kind": item.get("kind"),
                "artifact_type": item.get("artifact_type"),
                "name": item.get("name"),
                "path": item.get("path"),
                "available": item.get("available"),
            }
        )
    return artifacts


def _write_local_evidence(
    *,
    request: PrHandoffPackageRequest,
    package_data: dict[str, Any],
    output_paths: dict[str, Path | None],
    store: TaskMirrorStore | None = None,
) -> tuple[bool, bool]:
    json_path = output_paths["json_path"]
    markdown_path = output_paths["markdown_path"]
    if json_path is None or markdown_path is None:
        return False, False
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(package_data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    markdown = _build_markdown(package_data)
    markdown_path.write_text(markdown, encoding="utf-8")

    stored = store or TaskMirrorStore(request.db_path or DEFAULT_DB_PATH)
    artifact_recorded = _record_artifact_once(
        stored,
        request.task_key,
        json_path,
    )
    event_recorded = _record_event_once(
        stored,
        request.task_key,
        json_path=json_path,
        markdown_path=markdown_path,
        package_data=package_data,
    )
    return artifact_recorded, event_recorded


def _record_artifact_once(
    store: TaskMirrorStore,
    task_key: str,
    json_path: Path,
) -> bool:
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type == ARTIFACT_TYPE and artifact.path == json_path:
            return False
    store.record_task_artifact(task_key, ARTIFACT_TYPE, json_path)
    return True


def _record_event_once(
    store: TaskMirrorStore,
    task_key: str,
    *,
    json_path: Path,
    markdown_path: Path,
    package_data: dict[str, Any],
) -> bool:
    for event in store.list_task_events(task_key):
        if event.event_type == EVENT_TYPE:
            return False
    store.record_task_event(
        task_key,
        EVENT_TYPE,
        SOURCE,
        message="PR handoff package created",
        payload={
            "kind": EVENT_TYPE,
            "artifact_type": ARTIFACT_TYPE,
            "task_key": task_key,
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "dry_run_only": False,
            "branch_pushed": False,
            "pr_created": False,
            "requires_human_confirmation": True,
            "ready_for_branch_push_review": package_data["summary"]["ready_for_branch_push_review"],
            "ready_for_draft_pr_review": package_data["summary"]["ready_for_draft_pr_review"],
            "generated_at": package_data["generated_at"],
        },
    )
    return True


def _git(worktree_path: Path, args: list[str]) -> str:
    allowed = {
        ("rev-parse", "HEAD"),
        ("rev-parse", "--abbrev-ref", "HEAD"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("log", "--oneline", "-n", "5"),
    }
    if len(args) == 3 and args[0] == "diff" and args[1] in {"--name-only", "--stat"} and args[2].endswith("..HEAD"):
        pass
    elif tuple(args) not in allowed:
        raise PrHandoffPackageError(f"Git command is not allowed: git {' '.join(args)}")

    completed = subprocess.run(
        ["git", *args],
        cwd=worktree_path,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise PrHandoffPackageError(
            f"git {' '.join(args)} failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _safety_block(
    *,
    dry_run: bool,
    local_artifact_written: bool,
    local_event_recorded: bool,
) -> dict[str, Any]:
    return {
        "human_review_required": True,
        "read_only": dry_run,
        "read_only_git_remote": True,
        "task_status_changed": False,
        "db_written": local_artifact_written or local_event_recorded,
        "artifact_written": local_artifact_written,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
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


def _error_result(task_key: str, *, status: str, error: str) -> PrHandoffPackageResult:
    empty_review = {
        "ready_for_human_review": False,
        "blocking_warnings": [error],
        "non_blocking_warnings": [],
        "recommended_human_checks": [],
    }
    empty_source: dict[str, Any] = {
        "available": False,
        "kind": "github_issue",
        "artifact_type": "issue_spec",
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
    }
    empty_workspace: dict[str, Any] = {
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
    }
    empty_git = _unavailable_git_state(warnings=[error])
    empty_executor = {
        "available": False,
        "executor": None,
        "started_at": None,
        "finished_at": None,
        "finished_ok": False,
        "summary": None,
        "run_id": None,
        "runs": [],
    }
    empty_validation = {
        "available": False,
        "all_passed": False,
        "failed_validators": [],
        "results": [],
    }
    empty_evidence = {
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
    safety = _safety_block(
        dry_run=True,
        local_artifact_written=False,
        local_event_recorded=False,
    )
    return PrHandoffPackageResult(
        ok=False,
        status=status,
        task_key=task_key,
        task_status=None,
        review_summary=empty_review,
        source=empty_source,
        workspace=empty_workspace,
        git=empty_git,
        executor=empty_executor,
        validation=empty_validation,
        evidence=empty_evidence,
        handoff={
            "proposed_pr_title": None,
            "proposed_pr_body": None,
            "proposed_pr_base": None,
            "proposed_pr_head": None,
            "package_artifacts": [],
            "evidence_artifacts": [],
        },
        dry_run={
            "branch_push": _branch_push_preview(
                branch="",
                remote=DEFAULT_REMOTE,
                base_branch="",
                base_sha=None,
            ),
            "draft_pr": _draft_pr_preview(
                title="",
                body="",
                base_branch="",
                head_branch="",
            ),
        },
        next_allowed_actions=[],
        actions_not_performed=[
            "branch push",
            "PR creation",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        summary={
            "handoff_package_created": False,
            "ready_for_branch_push_review": False,
            "ready_for_draft_pr_review": False,
            "requires_human_confirmation": True,
            "next_phase": "blocked",
        },
        safety=safety,
        warnings=[error],
        artifact_recorded=False,
        event_recorded=False,
        package_json_path=None,
        package_markdown_path=None,
        generated_at=utc_now_iso(),
        error=error,
    )


__all__ = [
    "ARTIFACT_TYPE",
    "EVENT_TYPE",
    "PrHandoffPackageError",
    "PrHandoffPackageRequest",
    "PrHandoffPackageResult",
    "create_pr_handoff_package",
]
