"""Explicit draft PR creation confirmation from Phase 5B and Phase 5C evidence.

This module is intentionally narrow. It can create a GitHub draft PR only when
called with an explicit confirmation and ready local evidence.
It never merges, approves, cleans up, deletes branches or worktrees, pushes,
or mutates task status.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.draft_pr_confirm_helpers import (
    DraftPrConfirmError,
    PROTECTED_HEAD_BRANCHES,
    body_preview as _body_preview,
    build_gh_create_command as _build_gh_create_command,
    build_gh_list_open_pr_command as _build_gh_list_open_pr_command,
    build_gh_view_command as _build_gh_view_command,
    build_verification_result as _verification_result,
    command_preview as _command_preview,
    dedupe_preserve_order as _dedupe_preserve_order,
    empty_verification_preview as _empty_verification_preview,
    empty_verification_result as _empty_verification_result,
    extract_pr_commit_oids as _extract_pr_commit_oids,
    extract_pr_file_paths as _extract_pr_file_paths,
    extract_pr_url as _extract_pr_url,
    normalize_branch_choice as _normalize_branch_choice,
    normalize_repo as _normalize_repo,
    parse_event_payload as _parse_event_payload,
    parse_json_array as _parse_json_array,
    parse_json_object as _parse_json_object,
    stringify_list as _stringify_list,
)
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
DEFAULT_REMOTE = "origin"

__all__ = [
    "ARTIFACT_TYPE",
    "BRANCH_PUSH_ARTIFACT_TYPE",
    "DEFAULT_DB_PATH",
    "DEFAULT_REMOTE",
    "DraftPrConfirmError",
    "DraftPrConfirmRequest",
    "DraftPrConfirmResult",
    "EVENT_TYPE",
    "PROTECTED_HEAD_BRANCHES",
    "SOURCE",
    "confirm_draft_pr",
]


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
    verification_preview: dict[str, Any]
    verification: dict[str, Any]
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
            "verification_preview": self.verification_preview,
            "verification": self.verification,
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
    verification_preview: dict[str, Any] = _empty_verification_preview()
    verified_pr: dict[str, Any] = _empty_verification_result(expected=verification_preview)
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

        verification_preview = _build_verification_preview(
            request=request,
            worktree=worktree,
            handoff=handoff,
            resolved=resolved,
        )
        verified_pr = _empty_verification_result(expected=verification_preview)

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
            verified_pr = _view_and_verify_pr(
                repo=request.repo,
                pr_ref=existing_pr["url"] or existing_pr["number"],
                expected=verification_preview,
                runner=runner,
            )
            if verified_pr["passed"]:
                return _already_exists_verified_result(
                    request=request,
                    task=task,
                    handoff=handoff,
                    branch_push=branch_push,
                    existing_pr=existing_pr,
                    verification_preview=verification_preview,
                    verification=verified_pr,
                    base=resolved["base"],
                    head=resolved["head"],
                    title=resolved["title"],
                    body=resolved["body"],
                    preview_text=preview_text,
                    warnings=list(handoff.warnings) + branch_push["warnings"],
                )
            return _already_exists_result(
                request=request,
                task=task,
                handoff=handoff,
                branch_push=branch_push,
                existing_pr=existing_pr,
                verification_preview=verification_preview,
                verification=verified_pr,
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
                verification_preview=verification_preview,
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
        verified_pr = _view_and_verify_pr(
            repo=request.repo,
            pr_ref=pr_url,
            expected=verification_preview,
            runner=runner,
        )
        if not verified_pr["passed"]:
            return _verification_failed_result(
                request=request,
                task=task,
                handoff=handoff,
                branch_push=branch_push,
                existing_pr=existing_pr,
                verification_preview=verification_preview,
                verification=verified_pr,
                base=resolved["base"],
                head=resolved["head"],
                title=resolved["title"],
                body=resolved["body"],
                preview_text=preview_text,
                pr_url=pr_url,
            )
        pr_number = verified_pr.get("actual_number")
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
                verification_preview=verification_preview,
                verification=verified_pr,
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
            verification_preview=verification_preview,
            verification=verified_pr,
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
        verification=verified_pr,
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
        verification_preview=verification_preview,
        verification=verified_pr,
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
    command = _build_gh_list_open_pr_command(repo=repo, head=head)
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


def _build_verification_preview(
    *,
    request: DraftPrConfirmRequest,
    worktree: Any,
    handoff: Any,
    resolved: dict[str, str],
) -> dict[str, Any]:
    expected_files = list(handoff.git.get("changed_files", []))
    expected_commits = _expected_commits(
        worktree_path=worktree.worktree_path,
        base_sha=str(worktree.base_sha or "").strip(),
    )
    return {
        "required": True,
        "post_create_verification_required": True,
        "expected_repo": request.repo,
        "expected_base": resolved["base"],
        "expected_head": resolved["head"],
        "expected_title": resolved["title"],
        "expected_files": expected_files,
        "expected_commits": expected_commits,
        "expected_state": "OPEN",
        "expected_is_draft": True,
    }


def _view_and_verify_pr(
    *,
    repo: str,
    pr_ref: str | int | None,
    expected: dict[str, Any],
    runner: Runner | None,
) -> dict[str, Any]:
    if pr_ref is None or (isinstance(pr_ref, str) and not pr_ref.strip()):
        raise DraftPrConfirmError("GitHub PR reference is missing")
    pr_ref_text = str(pr_ref)
    completed = _run_command(
        _build_gh_view_command(repo, pr_ref_text),
        cwd=None,
        runner=runner,
    )
    if completed.returncode != 0:
        raise DraftPrConfirmError(
            f"gh pr view failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    payload = _parse_json_object(completed.stdout, source="gh pr view")
    _validate_view_payload(
        payload,
        repo=repo,
        expected_url=pr_ref_text if pr_ref_text.startswith("http") else None,
        base=str(expected["expected_base"]),
        head=str(expected["expected_head"]),
        title=str(expected["expected_title"]),
    )
    return _verification_result(payload, expected=expected)


def _expected_commits(*, worktree_path: Path, base_sha: str) -> list[str]:
    if not base_sha:
        raise DraftPrConfirmError("Base SHA is unavailable for PR verification")
    completed = _run_command(
        [
            "git",
            "-C",
            str(worktree_path),
            "log",
            "--format=%H",
            "--reverse",
            f"{base_sha}..HEAD",
        ],
        cwd=None,
        runner=None,
    )
    if completed.returncode != 0:
        raise DraftPrConfirmError(
            f"git log failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


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


def _validate_view_payload(
    payload: dict[str, Any],
    *,
    repo: str,
    expected_url: str | None,
    base: str,
    head: str,
    title: str,
) -> None:
    if not repo.strip():
        raise DraftPrConfirmError("repo must not be empty")


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
    verification: dict[str, Any],
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
        "verified": bool(verification.get("verified")),
        "verification": verification,
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
    verification_preview: dict[str, Any],
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
            "verified": False,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        verification_preview=verification_preview,
        verification=_empty_verification_result(expected=verification_preview),
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push["available"],
            "verification_recorded": False,
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
            "verified": False,
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
            "draft_pr_verified": False,
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
        dry_run=request.dry_run,
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
    verification_preview: dict[str, Any],
    verification: dict[str, Any],
    base: str,
    head: str,
    title: str,
    body: str,
    preview_text: str,
    warnings: list[str],
) -> DraftPrConfirmResult:
    return DraftPrConfirmResult(
        ok=False,
        status="existing_pr_verification_failed",
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
            "number": existing_pr.get("number"),
            "url": existing_pr.get("url"),
            "verified": False,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        verification_preview=verification_preview,
        verification=verification,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push["available"],
            "verification_recorded": False,
        },
        next_allowed_actions=[
            "manually inspect the open PR",
            "close the stale PR if needed",
            "fix branch or base hygiene",
            "rerun confirm_draft_pr after correction",
        ],
        actions_not_performed=[
            "draft PR creation",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
            "issue close",
            "task status update",
        ],
        summary={
            "pr_created": False,
            "draft_pr_created": False,
            "verified": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": "existing_pr_verification_failed",
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
            "draft_pr_verified": False,
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
        dry_run=request.dry_run,
        confirmation_required=not request.confirm_draft_pr,
        error=None,
    )


def _already_exists_verified_result(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    handoff: Any,
    branch_push: dict[str, Any],
    existing_pr: dict[str, Any],
    verification_preview: dict[str, Any],
    verification: dict[str, Any],
    base: str,
    head: str,
    title: str,
    body: str,
    preview_text: str,
    warnings: list[str],
) -> DraftPrConfirmResult:
    return DraftPrConfirmResult(
        ok=True,
        status="already_exists_verified",
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
            "number": existing_pr.get("number"),
            "url": existing_pr.get("url"),
            "verified": True,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        verification_preview=verification_preview,
        verification=verification,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push["available"],
            "verification_recorded": False,
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
            "issue close",
            "task status update",
        ],
        summary={
            "pr_created": False,
            "draft_pr_created": False,
            "verified": True,
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
            "draft_pr_verified": True,
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
        dry_run=request.dry_run,
        confirmation_required=False,
        error=None,
    )


def _success_result(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    handoff: Any,
    branch_push: dict[str, Any],
    existing_pr: dict[str, Any],
    verification_preview: dict[str, Any],
    verification: dict[str, Any],
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
            "verified": True,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": str(artifact_path),
            "issue_closed": False,
        },
        verification_preview=verification_preview,
        verification=verification,
        evidence={
            "artifact_recorded": True,
            "event_recorded": True,
            "branch_push_verified": branch_push["available"],
            "verification_recorded": True,
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
            "verified": True,
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
            "draft_pr_verified": True,
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


def _verification_failed_result(
    *,
    request: DraftPrConfirmRequest,
    task: Any,
    handoff: Any,
    branch_push: dict[str, Any],
    existing_pr: dict[str, Any],
    verification_preview: dict[str, Any],
    verification: dict[str, Any],
    base: str,
    head: str,
    title: str,
    body: str,
    preview_text: str,
    pr_url: str,
) -> DraftPrConfirmResult:
    draft_number = verification.get("actual_number")
    if not isinstance(draft_number, int):
        draft_number = None
    return DraftPrConfirmResult(
        ok=False,
        status="pr_created_verification_failed",
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
            "number": draft_number,
            "url": pr_url,
            "verified": False,
            "title": title,
            "body_preview": _body_preview(body),
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        verification_preview=verification_preview,
        verification=verification,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push["available"],
            "verification_recorded": False,
        },
        next_allowed_actions=[
            "manually inspect the created PR",
            "close the stale PR if needed",
            "fix branch or base hygiene",
            "rerun confirm_draft_pr after correction",
        ],
        actions_not_performed=[
            "draft PR artifact recording",
            "draft PR event recording",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
            "issue close",
            "task status update",
        ],
        summary={
            "pr_created": True,
            "draft_pr_created": False,
            "verified": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": "pr_created_verification_failed",
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
            "draft_pr_verified": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
        },
        warnings=[
            *list(handoff.warnings),
            *branch_push["warnings"],
            *verification.get("blocking_warnings", []),
        ],
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
    verification_preview: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
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
            "verified": False,
            "title": title,
            "body_preview": body_preview,
            "body_path": str(request.body_file) if request.body_file is not None else None,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "command_preview": preview_text,
            "artifact_path": None,
            "issue_closed": False,
        },
        verification_preview=verification_preview or _empty_verification_preview(),
        verification=verification or _empty_verification_result(
            expected=verification_preview or _empty_verification_preview()
        ),
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_verified": branch_push.get("available", False),
            "verification_recorded": False,
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
            "draft_pr_verified": False,
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


