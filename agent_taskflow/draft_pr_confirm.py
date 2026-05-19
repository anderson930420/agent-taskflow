"""Explicit draft PR creation confirmation from Phase 5B and Phase 5C evidence.

This module is intentionally narrow. It can create a GitHub draft PR only when
called with an explicit confirmation and ready waiting-approval evidence.
It never merges, approves, cleans up, deletes branches or worktrees, pushes,
or mutates task status.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.models import utc_now_iso
from agent_taskflow.pr_handoff_package import (
    PrHandoffPackageRequest,
    create_pr_handoff_package,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


DEFAULT_DB_PATH = Path.home() / ".agent-taskflow" / "state.db"
ARTIFACT_TYPE = "draft_pr"
EVENT_TYPE = "draft_pr_created"
SOURCE = "draft_pr_confirm"
BRANCH_PUSH_ARTIFACT_TYPE = "branch_push"
PROTECTED_HEAD_BRANCHES = {"main", "master"}
DEFAULT_REMOTE = "origin"


class DraftPrConfirmError(RuntimeError):
    """Raised when a draft PR cannot be safely created."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class DraftPrConfirmRequest:
    """Request for previewing or confirming a draft PR."""

    task_key: str
    repo: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    base: str | None = None
    head: str | None = None
    title: str | None = None
    body_file: Path | None = None
    dry_run: bool = False
    confirm_draft_pr: bool = False
    allow_non_waiting: bool = False
    remote: str = DEFAULT_REMOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "repo", _normalize_repo(self.repo))
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
        if self.body_file is not None:
            object.__setattr__(
                self,
                "body_file",
                ensure_absolute_path(self.body_file, name="body_file"),
            )
        normalized_remote = self.remote.strip()
        if not normalized_remote:
            raise ValueError("remote must not be empty")
        if normalized_remote.startswith("-") or any(ch.isspace() for ch in normalized_remote):
            raise ValueError("remote must be a simple git remote name")
        object.__setattr__(self, "remote", normalized_remote)


@dataclass(frozen=True)
class DraftPrConfirmResult:
    """Structured preview or confirmation result for a draft PR."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    repo: str
    base: str | None
    head: str | None
    title: str | None
    body_preview: str | None
    handoff: dict[str, Any]
    branch_push: dict[str, Any]
    existing_pr: dict[str, Any]
    draft_pr: dict[str, Any]
    evidence: dict[str, Any]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    summary: dict[str, Any]
    safety: dict[str, Any]
    warnings: list[str]
    performed: bool
    dry_run: bool
    confirmation_required: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "status": self.status,
            "task_key": self.task_key,
            "task_status": self.task_status,
            "repo": self.repo,
            "base": self.base,
            "head": self.head,
            "title": self.title,
            "body_preview": self.body_preview,
            "handoff": self.handoff,
            "branch_push": self.branch_push,
            "existing_pr": self.existing_pr,
            "draft_pr": self.draft_pr,
            "evidence": self.evidence,
            "next_allowed_actions": self.next_allowed_actions,
            "actions_not_performed": self.actions_not_performed,
            "summary": self.summary,
            "safety": self.safety,
            "warnings": self.warnings,
            "performed": self.performed,
            "dry_run": self.dry_run,
            "confirmation_required": self.confirmation_required,
            "error": self.error,
        }
        return json.loads(json.dumps(payload, sort_keys=True))


def confirm_draft_pr(
    request: DraftPrConfirmRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> DraftPrConfirmResult:
    """Preview or create a GitHub draft PR from ready local evidence."""

    db_path = request.db_path or DEFAULT_DB_PATH
    if not db_path.exists():
        return _error_result(
            request=request,
            status="not_found",
            error=f"SQLite state DB not found: {db_path}",
            existing_pr=_empty_existing_pr(),
            branch_push=_empty_branch_push(),
        )

    current_store = store or TaskMirrorStore(db_path)
    handoff_request = PrHandoffPackageRequest(
        task_key=request.task_key,
        repo_path=request.repo_path,
        db_path=db_path,
        artifact_root=request.artifact_root,
        dry_run=True,
        allow_non_waiting=request.allow_non_waiting,
    )
    handoff = create_pr_handoff_package(handoff_request, store=current_store)
    if not handoff.ok:
        return _error_result(
            request=request,
            status=handoff.status,
            error=handoff.error or handoff.summary.get("next_phase") or "Draft PR handoff is not ready",
            existing_pr=_empty_existing_pr(),
            branch_push=_empty_branch_push(),
            handoff=_handoff_snapshot(handoff),
            warnings=list(handoff.warnings),
        )

    task = current_store.get_task(request.task_key)
    if task is None:
        return _error_result(
            request=request,
            status="not_found",
            error=f"Task not found: {request.task_key}",
            existing_pr=_empty_existing_pr(),
            branch_push=_empty_branch_push(),
            handoff=_handoff_snapshot(handoff),
            warnings=list(handoff.warnings),
        )

    worktree = current_store.get_task_worktree(request.task_key)
    if worktree is None:
        return _error_result(
            request=request,
            status="blocked",
            error=f"TaskWorktreeRecord missing for task: {request.task_key}",
            existing_pr=_empty_existing_pr(),
            branch_push=_empty_branch_push(),
            handoff=_handoff_snapshot(handoff),
            warnings=list(handoff.warnings),
        )

    branch_push = _empty_branch_push()
    existing_pr = _empty_existing_pr()
    preview_text: str | None = None
    resolved: dict[str, str] | None = None
    try:
        validations = _validate_local_readiness(
            request=request,
            task=task,
            worktree=worktree,
            handoff=handoff,
        )
        if validations:
            return _error_result(
                request=request,
                status="blocked",
                error=validations[0],
                existing_pr=existing_pr,
                branch_push=branch_push,
                handoff=_handoff_snapshot(handoff),
                warnings=list(handoff.warnings) + validations,
            )

        resolved = _resolve_target(
            request=request,
            task=task,
            worktree=worktree,
            handoff=handoff,
        )

        branch_push = _read_branch_push_evidence(
            current_store,
            request.task_key,
            expected_branch=resolved["head"],
            expected_base_branch=resolved["base"],
        )
        if not branch_push["available"]:
            return _error_result(
                request=request,
                status="blocked",
                error=branch_push["warnings"][0] if branch_push["warnings"] else "Missing branch push evidence",
                existing_pr=existing_pr,
                branch_push=branch_push,
                handoff=_handoff_snapshot(handoff),
                warnings=list(handoff.warnings) + branch_push["warnings"],
            )

        preview_command = _build_gh_create_command(
            repo=request.repo,
            base=resolved["base"],
            head=resolved["head"],
            title=resolved["title"],
            body=resolved["body"],
        )
        preview_text = _command_preview(preview_command)

        existing_pr = _check_existing_open_pr(
            repo=request.repo,
            head=resolved["head"],
            runner=runner,
        )
        if existing_pr["exists"]:
            return _already_exists_result(
                request=request,
                task=task,
                handoff=handoff,
                branch_push=branch_push,
                existing_pr=existing_pr,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
                preview_text=preview_text,
                warnings=list(handoff.warnings) + branch_push["warnings"],
            )

        if request.dry_run:
            return _preview_result(
                request=request,
                task=task,
                handoff=handoff,
                branch_push=branch_push,
                existing_pr=existing_pr,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
                preview_text=preview_text,
                warnings=list(handoff.warnings) + branch_push["warnings"],
            )

        if not request.confirm_draft_pr:
            return _error_result(
                request=request,
                status="blocked",
                error="Draft PR creation requires --confirm-draft-pr",
                existing_pr=existing_pr,
                branch_push=branch_push,
                handoff=_handoff_snapshot(handoff),
                warnings=list(handoff.warnings) + branch_push["warnings"],
                preview_text=preview_text,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
            )

        create_completed = _run_command(
            _build_gh_create_command(
                repo=request.repo,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
            ),
            cwd=worktree.worktree_path,
            runner=runner,
        )
        if create_completed.returncode != 0:
            return _error_result(
                request=request,
                status="blocked",
                error=f"gh pr create failed with {create_completed.returncode}: {create_completed.stderr.strip()}",
                existing_pr=existing_pr,
                branch_push=branch_push,
                handoff=_handoff_snapshot(handoff),
                warnings=list(handoff.warnings) + branch_push["warnings"],
                preview_text=preview_text,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
            )

        pr_url = _extract_pr_url(create_completed.stdout)
        view_completed = _run_command(
            _build_gh_view_command(request.repo, pr_url),
            cwd=worktree.worktree_path,
            runner=runner,
        )
        if view_completed.returncode != 0:
            return _error_result(
                request=request,
                status="blocked",
                error=f"gh pr view failed with {view_completed.returncode}: {view_completed.stderr.strip()}",
                existing_pr=existing_pr,
                branch_push=branch_push,
                handoff=_handoff_snapshot(handoff),
                warnings=list(handoff.warnings) + branch_push["warnings"],
                preview_text=preview_text,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
            )

        view_payload = _parse_json_object(view_completed.stdout, source="gh pr view")
        _validate_view_payload(
            view_payload,
            repo=request.repo,
            expected_url=pr_url,
            base=resolved["base"],
            head=resolved["head"],
            title=resolved["title"],
            body=resolved["body"],
        )
        pr_number = view_payload.get("number")
        if not isinstance(pr_number, int):
            return _error_result(
                request=request,
                status="blocked",
                error="GitHub response missing numeric PR number",
                existing_pr=existing_pr,
                branch_push=branch_push,
                handoff=_handoff_snapshot(handoff),
                warnings=list(handoff.warnings) + branch_push["warnings"],
                preview_text=preview_text,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
            )
    except DraftPrConfirmError as exc:
        return _error_result(
            request=request,
            status="blocked",
            error=str(exc),
            existing_pr=existing_pr,
            branch_push=branch_push,
            handoff=_handoff_snapshot(handoff),
            warnings=list(handoff.warnings) + branch_push["warnings"],
            preview_text=preview_text,
            base=resolved["base"] if resolved else None,
            head=resolved["head"] if resolved else None,
            title=resolved["title"] if resolved else None,
            body=resolved["body"] if resolved else None,
        )

    artifact_path = _draft_pr_path(
        request=request,
        task=task,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    evidence = _draft_pr_evidence(
        task_key=request.task_key,
        repo=request.repo,
        base=resolved["base"],
        head=resolved["head"],
        title=resolved["title"],
        body=resolved["body"],
        pr_number=pr_number,
        pr_url=pr_url,
        branch_push=branch_push,
        created_at=utc_now_iso(),
        body_file=request.body_file,
    )
    artifact_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_recorded = current_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, artifact_path)
    event_recorded = current_store.record_task_event(
        request.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Draft PR created",
        payload=evidence,
    )
    # The store helpers return None; treat successful execution as recorded.
    _ = artifact_recorded, event_recorded

    return _success_result(
        request=request,
        task=task,
        handoff=handoff,
        branch_push=branch_push,
        existing_pr=existing_pr,
        base=resolved["base"],
        head=resolved["head"],
        title=resolved["title"],
        body=resolved["body"],
        preview_text=preview_text,
        pr_number=pr_number,
        pr_url=pr_url,
        artifact_path=artifact_path,
        warnings=list(handoff.warnings) + branch_push["warnings"],
    )


def _validate_local_readiness(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    worktree: Any,
    handoff: Any,
) -> list[str]:
    warnings: list[str] = list(handoff.warnings)
    if task.status != "waiting_approval":
        if request.dry_run and request.allow_non_waiting:
            pass
        else:
            warnings.append(f"Task {task.task_key} must be waiting_approval, got {task.status}")

    if not handoff.summary.get("ready_for_draft_pr_review"):
        warnings.append("Phase 5B handoff is not ready for draft PR review")

    if handoff.review_summary.get("blocking_warnings"):
        warnings.extend(str(item) for item in handoff.review_summary["blocking_warnings"])

    if not handoff.source.get("available"):
        warnings.append("Source evidence is missing")
    if not handoff.workspace.get("available"):
        warnings.append("Worktree evidence is missing")
    if not handoff.executor.get("available") or not handoff.executor.get("finished_ok"):
        warnings.append("Executor evidence is missing or did not succeed")
    if not handoff.validation.get("available") or not handoff.validation.get("all_passed"):
        warnings.append("Validator evidence is missing or did not pass")

    proposed_title = handoff.handoff.get("proposed_pr_title")
    proposed_body = handoff.handoff.get("proposed_pr_body")
    proposed_base = handoff.handoff.get("proposed_pr_base")
    proposed_head = handoff.handoff.get("proposed_pr_head")
    if not isinstance(proposed_title, str) or not proposed_title.strip():
        warnings.append("Proposed PR title is missing")
    if not isinstance(proposed_body, str) or not proposed_body.strip():
        warnings.append("Proposed PR body is missing")
    if not isinstance(proposed_base, str) or not proposed_base.strip():
        warnings.append("Proposed PR base branch is missing")
    if not isinstance(proposed_head, str) or not proposed_head.strip():
        warnings.append("Proposed PR head branch is missing")

    if Path(task.repo_path).resolve() != request.repo_path.resolve():
        warnings.append(
            f"Provided repo_path {request.repo_path} does not match the task repo_path {task.repo_path}"
        )
    if worktree.worktree_path and not worktree.worktree_path.exists():
        warnings.append(f"Worktree path is missing on disk: {worktree.worktree_path}")

    return _dedupe_preserve_order(warnings)


def _resolve_target(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    worktree: Any,
    handoff: Any,
) -> dict[str, str]:
    base = _normalize_branch_choice(
        provided=request.base,
        canonical=str(handoff.handoff.get("proposed_pr_base") or worktree.base_branch or "").strip(),
        field_name="base",
    )
    head = _normalize_branch_choice(
        provided=request.head,
        canonical=str(handoff.handoff.get("proposed_pr_head") or worktree.branch or "").strip(),
        field_name="head",
    )
    if head in PROTECTED_HEAD_BRANCHES:
        raise DraftPrConfirmError(f"Head branch must not be protected: {head}")

    title = request.title.strip() if request.title is not None else str(handoff.handoff.get("proposed_pr_title") or "").strip()
    if not title:
        raise DraftPrConfirmError("Proposed PR title is required")

    body = _load_body_text(request, handoff)
    if not body.strip():
        raise DraftPrConfirmError("Proposed PR body is required")

    repo = request.repo.strip()
    source_repo = str(handoff.source.get("repo") or "").strip()
    if source_repo and source_repo != repo:
        raise DraftPrConfirmError(f"Repo target {repo!r} does not match handoff repo {source_repo!r}")

    if Path(task.repo_path).resolve() != request.repo_path.resolve():
        raise DraftPrConfirmError(
            f"Provided repo_path {request.repo_path} does not match task repo_path {task.repo_path}"
        )

    return {
        "base": base,
        "head": head,
        "title": title,
        "body": body,
    }


def _load_body_text(request: DraftPrConfirmRequest, handoff: Any) -> str:
    if request.body_file is not None:
        try:
            return request.body_file.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive runtime guard
            raise DraftPrConfirmError(f"Could not read body file: {exc}") from exc
    body = handoff.handoff.get("proposed_pr_body")
    if not isinstance(body, str):
        return ""
    return body


def _normalize_branch_choice(*, provided: str | None, canonical: str, field_name: str) -> str:
    if not canonical:
        raise DraftPrConfirmError(f"Missing canonical {field_name} branch")
    if provided is None:
        return canonical
    normalized = provided.strip()
    if not normalized:
        raise DraftPrConfirmError(f"{field_name} must not be empty")
    if normalized != canonical:
        raise DraftPrConfirmError(
            f"Provided {field_name} branch {normalized!r} does not match the ready handoff branch {canonical!r}"
        )
    return normalized


def _read_branch_push_evidence(
    store: TaskMirrorStore,
    task_key: str,
    *,
    expected_branch: str,
    expected_base_branch: str,
) -> dict[str, Any]:
    warnings: list[str] = []
    events = [
        event
        for event in store.list_task_events(task_key)
        if event.event_type == "branch_push_completed"
    ]
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == BRANCH_PUSH_ARTIFACT_TYPE
    ]

    if not events:
        warnings.append("branch_push_completed event is missing")
    if not artifacts:
        warnings.append("draft PR evidence artifact is missing for branch push")

    payload: dict[str, Any] = {}
    if events:
        payload = _parse_event_payload(events[-1].payload_json, event_type="branch_push_completed")

    if payload.get("branch") and payload.get("branch") != expected_branch:
        warnings.append(
            f"branch push evidence branch {payload.get('branch')!r} does not match expected branch {expected_branch!r}"
        )
    if payload.get("base_branch") and payload.get("base_branch") != expected_base_branch:
        warnings.append(
            f"branch push evidence base branch {payload.get('base_branch')!r} does not match expected base branch {expected_base_branch!r}"
        )

    expected_flags = {
        "branch_pushed": True,
        "push_ok": True,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
    }
    for field_name, expected in expected_flags.items():
        if payload.get(field_name) is not expected:
            warnings.append(f"branch push evidence {field_name} must be {expected}")

    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    for field_name, expected in {
        "branch_pushed": True,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
    }.items():
        if safety.get(field_name) is not expected:
            warnings.append(f"branch push evidence safety.{field_name} must be {expected}")

    available = not warnings and bool(events) and bool(artifacts)
    artifact_path = str(artifacts[-1].path) if artifacts else None
    return {
        "available": available,
        "event_recorded": bool(events),
        "artifact_recorded": bool(artifacts),
        "event_type": "branch_push_completed",
        "artifact_kind": BRANCH_PUSH_ARTIFACT_TYPE,
        "artifact_path": artifact_path,
        "branch_pushed": bool(payload.get("branch_pushed")),
        "push_ok": bool(payload.get("push_ok")),
        "pr_created": bool(payload.get("pr_created")),
        "merged": bool(payload.get("merged")),
        "approved": bool(payload.get("approved")),
        "cleanup_performed": bool(payload.get("cleanup_performed")),
        "branch": payload.get("branch"),
        "base_branch": payload.get("base_branch"),
        "head_sha": payload.get("head_sha"),
        "safety": safety,
        "warnings": warnings,
    }


def _check_existing_open_pr(
    *,
    repo: str,
    head: str,
    runner: Runner | None,
) -> dict[str, Any]:
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        head,
        "--state",
        "open",
        "--json",
        "number,url,state,isDraft,title",
    ]
    completed = _run_command(command, cwd=None, runner=runner)
    if completed.returncode != 0:
        raise DraftPrConfirmError(
            f"gh pr list failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    payload = _parse_json_array(completed.stdout, source="gh pr list")
    existing_pr = None
    if payload:
        existing_pr = payload[0] if isinstance(payload[0], dict) else None
    if existing_pr is None:
        return {
            "checked": True,
            "exists": False,
            "number": None,
            "url": None,
            "state": None,
            "is_draft": None,
            "title": None,
            "command_preview": _command_preview(command),
        }
    return {
        "checked": True,
        "exists": True,
        "number": existing_pr.get("number"),
        "url": existing_pr.get("url"),
        "state": existing_pr.get("state"),
        "is_draft": existing_pr.get("isDraft"),
        "title": existing_pr.get("title"),
        "command_preview": _command_preview(command),
    }


def _build_gh_create_command(*, repo: str, base: str, head: str, title: str, body: str) -> list[str]:
    return [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body",
        body,
        "--draft",
    ]


def _build_gh_view_command(repo: str, pr_url: str) -> list[str]:
    return [
        "gh",
        "pr",
        "view",
        pr_url,
        "--repo",
        repo,
        "--json",
        "url,number,headRefName,baseRefName,isDraft,title,body,state",
    ]


def _run_command(
    command: list[str],
    *,
    cwd: Path | None,
    runner: Runner | None,
) -> CompletedProcessLike:
    try:
        return (runner or subprocess.run)(
            command,
            cwd=cwd,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:  # pragma: no cover - defensive runtime guard
        raise DraftPrConfirmError(str(exc)) from exc


def _parse_json_object(stdout: str, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DraftPrConfirmError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DraftPrConfirmError(f"{source} returned non-object JSON")
    return payload


def _parse_json_array(stdout: str, *, source: str) -> list[Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DraftPrConfirmError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise DraftPrConfirmError(f"{source} returned non-array JSON")
    return payload


def _parse_event_payload(payload_json: str | None, *, event_type: str) -> dict[str, Any]:
    if not payload_json:
        return {}
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("kind") not in {event_type, None}:
        return {}
    return payload


def _validate_view_payload(
    payload: dict[str, Any],
    *,
    repo: str,
    expected_url: str,
    base: str,
    head: str,
    title: str,
    body: str,
) -> None:
    if payload.get("isDraft") is not True:
        raise DraftPrConfirmError("GitHub response did not confirm a draft PR")
    verified_url = payload.get("url")
    if verified_url != expected_url:
        raise DraftPrConfirmError("GitHub verification URL did not match created PR URL")
    if payload.get("headRefName") != head:
        raise DraftPrConfirmError("GitHub response headRefName did not match requested head")
    if payload.get("baseRefName") != base:
        raise DraftPrConfirmError("GitHub response baseRefName did not match requested base")
    if payload.get("title") != title:
        raise DraftPrConfirmError("GitHub response title did not match requested title")
    body_value = payload.get("body")
    if not isinstance(body_value, str) or not body_value.strip():
        raise DraftPrConfirmError("GitHub response body was missing")
    state = str(payload.get("state") or "").strip().upper()
    if state and state != "OPEN":
        raise DraftPrConfirmError("GitHub response state did not remain open")
    if not repo.strip():
        raise DraftPrConfirmError("repo must not be empty")


def _extract_pr_url(stdout: str) -> str:
    for match in re.findall(r"https://github\.com/[^\s]+/pull/\d+", stdout):
        return match.rstrip()
    raise DraftPrConfirmError("gh pr create did not print a created PR URL")


def _draft_pr_path(*, request: DraftPrConfirmRequest, task: Any) -> Path:
    if request.artifact_root is not None:
        return request.artifact_root / "draft_pr" / request.task_key / "draft_pr.json"
    artifact_dir = getattr(task, "artifact_dir", None)
    if artifact_dir:
        return Path(artifact_dir).expanduser().resolve().parent / "draft_pr" / request.task_key / "draft_pr.json"
    return request.repo_path / ".agent-taskflow" / "artifacts" / request.task_key / "draft_pr.json"


def _draft_pr_evidence(
    *,
    task_key: str,
    repo: str,
    base: str,
    head: str,
    title: str,
    body: str,
    pr_number: int,
    pr_url: str,
    branch_push: dict[str, Any],
    created_at: str,
    body_file: Path | None,
) -> dict[str, Any]:
    return {
        "kind": EVENT_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": task_key,
        "repo": repo,
        "base_branch": base,
        "head_branch": head,
        "title": title,
        "body": body,
        "body_path": str(body_file) if body_file is not None else None,
        "body_preview": _body_preview(body),
        "draft": True,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "branch_push_verified": branch_push["available"],
        "branch_push_artifact_path": branch_push.get("artifact_path"),
        "branch_push_event_type": branch_push["event_type"],
        "created_at": created_at,
        "pr_created": True,
        "draft_pr_created": True,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "issue_closed": False,
        "requires_human_confirmation": True,
        "safety": {
            "human_confirmation_required": True,
            "human_confirmation_confirmed": True,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "branch_push_required_before_pr": True,
            "pr_created": True,
            "draft_pr": True,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
        },
    }


def _body_preview(text: str, *, limit: int = 240) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:limit]


def _command_preview(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _normalize_repo(repo: str) -> str:
    normalized = repo.strip()
    if not normalized:
        raise ValueError("repo must not be empty")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise ValueError("repo must be a simple owner/name string")
    if normalized.count("/") != 1:
        raise ValueError("repo must be an owner/name string")
    return normalized


def _handoff_snapshot(handoff: Any) -> dict[str, Any]:
    return {
        "ready_for_draft_pr_review": bool(handoff.summary.get("ready_for_draft_pr_review")),
        "ready_for_branch_push_review": bool(handoff.summary.get("ready_for_branch_push_review")),
        "blocking_warnings": list(handoff.review_summary.get("blocking_warnings", [])),
        "source_available": bool(handoff.source.get("available")),
        "workspace_available": bool(handoff.workspace.get("available")),
        "executor_available": bool(handoff.executor.get("available")),
        "executor_finished_ok": bool(handoff.executor.get("finished_ok")),
        "validation_available": bool(handoff.validation.get("available")),
        "validation_all_passed": bool(handoff.validation.get("all_passed")),
        "proposed_pr_title": handoff.handoff.get("proposed_pr_title"),
        "proposed_pr_body": handoff.handoff.get("proposed_pr_body"),
        "proposed_pr_base": handoff.handoff.get("proposed_pr_base"),
        "proposed_pr_head": handoff.handoff.get("proposed_pr_head"),
    }


def _empty_existing_pr() -> dict[str, Any]:
    return {
        "checked": False,
        "exists": False,
        "number": None,
        "url": None,
        "state": None,
        "is_draft": None,
        "title": None,
        "command_preview": None,
    }


def _empty_branch_push() -> dict[str, Any]:
    return {
        "available": False,
        "event_recorded": False,
        "artifact_recorded": False,
        "event_type": "branch_push_completed",
        "artifact_kind": "branch_push",
        "artifact_path": None,
        "branch_pushed": False,
        "push_ok": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "branch": None,
        "base_branch": None,
        "head_sha": None,
        "safety": {},
        "warnings": [],
    }


def _preview_result(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    handoff: Any,
    branch_push: dict[str, Any],
    existing_pr: dict[str, Any],
    base: str,
    head: str,
    title: str,
    body: str,
    preview_text: str,
    warnings: list[str],
) -> DraftPrConfirmResult:
    return DraftPrConfirmResult(
        ok=True,
        status="dry_run",
        task_key=request.task_key,
        task_status=task.status,
        repo=request.repo,
        base=base,
        head=head,
        title=title,
        body_preview=_body_preview(body),
        handoff=_handoff_snapshot(handoff),
        branch_push=branch_push,
        existing_pr=existing_pr,
        draft_pr={
            "created": False,
            "draft": True,
            "number": None,
            "url": None,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push["available"],
        },
        next_allowed_actions=[
            "manual review of draft PR",
            "human merge decision outside Agent Taskflow",
            "post-merge cleanup recommendation in later phase",
        ],
        actions_not_performed=[
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        summary={
            "pr_created": False,
            "draft_pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": "manual_review_of_draft_pr",
        },
        safety={
            "human_confirmation_required": True,
            "human_confirmation_confirmed": False,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "branch_push_required_before_pr": True,
            "pr_created": False,
            "draft_pr": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
        },
        warnings=warnings,
        performed=False,
        dry_run=True,
        confirmation_required=not request.confirm_draft_pr,
        error=None,
    )


def _already_exists_result(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    handoff: Any,
    branch_push: dict[str, Any],
    existing_pr: dict[str, Any],
    base: str,
    head: str,
    title: str,
    body: str,
    preview_text: str,
    warnings: list[str],
) -> DraftPrConfirmResult:
    return DraftPrConfirmResult(
        ok=True,
        status="already_exists",
        task_key=request.task_key,
        task_status=task.status,
        repo=request.repo,
        base=base,
        head=head,
        title=title,
        body_preview=_body_preview(body),
        handoff=_handoff_snapshot(handoff),
        branch_push=branch_push,
        existing_pr=existing_pr,
        draft_pr={
            "created": False,
            "draft": True,
            "number": None,
            "url": None,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push["available"],
        },
        next_allowed_actions=[
            "manual review of the existing open PR",
            "human merge decision outside Agent Taskflow",
            "post-merge cleanup recommendation in later phase",
        ],
        actions_not_performed=[
            "draft PR creation",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        summary={
            "pr_created": False,
            "draft_pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": "manual_review_of_existing_pr",
        },
        safety={
            "human_confirmation_required": True,
            "human_confirmation_confirmed": False,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "branch_push_required_before_pr": True,
            "pr_created": False,
            "draft_pr": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
        },
        warnings=warnings,
        performed=False,
        dry_run=False,
        confirmation_required=not request.confirm_draft_pr,
        error=None,
    )


def _success_result(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    handoff: Any,
    branch_push: dict[str, Any],
    existing_pr: dict[str, Any],
    base: str,
    head: str,
    title: str,
    body: str,
    preview_text: str,
    pr_number: int,
    pr_url: str,
    artifact_path: Path,
    warnings: list[str],
) -> DraftPrConfirmResult:
    return DraftPrConfirmResult(
        ok=True,
        status="draft_pr_created",
        task_key=request.task_key,
        task_status=task.status,
        repo=request.repo,
        base=base,
        head=head,
        title=title,
        body_preview=_body_preview(body),
        handoff=_handoff_snapshot(handoff),
        branch_push=branch_push,
        existing_pr=existing_pr,
        draft_pr={
            "created": True,
            "draft": True,
            "number": pr_number,
            "url": pr_url,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": str(artifact_path),
            "issue_closed": False,
        },
        evidence={
            "artifact_recorded": True,
            "event_recorded": True,
            "branch_push_verified": branch_push["available"],
        },
        next_allowed_actions=[
            "manual review of draft PR",
            "human merge decision outside Agent Taskflow",
            "post-merge cleanup recommendation in later phase",
        ],
        actions_not_performed=[
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        summary={
            "pr_created": True,
            "draft_pr_created": True,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": "post_merge_cleanup_recommendation",
        },
        safety={
            "human_confirmation_required": True,
            "human_confirmation_confirmed": True,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "branch_push_required_before_pr": True,
            "pr_created": True,
            "draft_pr": True,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
        },
        warnings=warnings,
        performed=True,
        dry_run=False,
        confirmation_required=False,
        error=None,
    )


def _error_result(
    *,
    request: DraftPrConfirmRequest,
    status: str,
    error: str,
    existing_pr: dict[str, Any],
    branch_push: dict[str, Any],
    handoff: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    preview_text: str | None = None,
    base: str | None = None,
    head: str | None = None,
    title: str | None = None,
    body: str | None = None,
) -> DraftPrConfirmResult:
    resolved_warnings = warnings or []
    body_preview = _body_preview(body) if body is not None else None
    return DraftPrConfirmResult(
        ok=False,
        status=status,
        task_key=request.task_key,
        task_status=None,
        repo=request.repo,
        base=base,
        head=head,
        title=title,
        body_preview=body_preview,
        handoff=handoff or _empty_handoff_snapshot(),
        branch_push=branch_push,
        existing_pr=existing_pr,
        draft_pr={
            "created": False,
            "draft": True,
            "number": None,
            "url": None,
            "title": title,
            "body_preview": body_preview,
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push.get("available", False),
        },
        next_allowed_actions=[
            "resolve blocking warnings",
            "rerun the draft PR confirmation command after evidence is complete",
        ],
        actions_not_performed=[
            "draft PR creation",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        summary={
            "pr_created": False,
            "draft_pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": "draft_pr_readiness_remediation",
        },
        safety={
            "human_confirmation_required": True,
            "human_confirmation_confirmed": False,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "branch_push_required_before_pr": True,
            "pr_created": False,
            "draft_pr": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
        },
        warnings=resolved_warnings,
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.confirm_draft_pr,
        error=error,
    )


def _empty_handoff_snapshot() -> dict[str, Any]:
    return {
        "ready_for_draft_pr_review": False,
        "ready_for_branch_push_review": False,
        "blocking_warnings": [],
        "source_available": False,
        "workspace_available": False,
        "executor_available": False,
        "executor_finished_ok": False,
        "validation_available": False,
        "validation_all_passed": False,
        "proposed_pr_title": None,
        "proposed_pr_body": None,
        "proposed_pr_base": None,
        "proposed_pr_head": None,
    }


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
