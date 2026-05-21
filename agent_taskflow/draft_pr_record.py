"""Record draft_pr evidence for a pre-existing GitHub PR.

This module records ``draft_pr`` artifact + ``draft_pr_created`` event in
the Agent Taskflow store for a PR that was created out-of-band (for
example, by hand after a Phase 6E+4 dry-run) so that the post-merge
cleanup chain (``recommend_post_merge_cleanup``,
``confirm_local_cleanup``, ``confirm_remote_branch_cleanup``, and
``confirm_task_closeout``) can read the evidence it requires.

It NEVER creates, edits, merges, closes, approves, or cleans up a PR.
It never pushes branches, deletes worktrees, deletes branches, or
mutates GitHub state in any way. It only:

* fetches the PR's existing state via ``gh pr view`` (read-only)
* fetches the ahead-commits/files via ``gh api repos/.../compare/...``
  (read-only)
* validates that the PR's metadata matches the local handoff evidence
  (base, head, headRefOid, title)
* validates that the compare diff matches the handoff's expected files
  and commits
* on explicit confirmation, writes ``draft_pr.json`` under
  ``<artifact-root>/draft_pr/<task-key>/`` and records the
  ``draft_pr_created`` task event

Use ``--target-repo`` + ``--allow-source-repo-mismatch`` for dogfood
tasks whose handoff source repo is a synthetic fixture but whose actual
PR lives on the real GitHub repo (see Phase 6E+4.1).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.draft_pr_confirm import (
    ARTIFACT_TYPE,
    BRANCH_PUSH_ARTIFACT_TYPE,
    DEFAULT_DB_PATH,
    EVENT_TYPE,
)
from agent_taskflow.draft_pr_confirm_helpers import (
    DraftPrConfirmError,
    PROTECTED_HEAD_BRANCHES,
    body_preview as _body_preview,
    build_gh_compare_command as _build_gh_compare_command,
    build_gh_view_command as _build_gh_view_command,
    build_verification_result as _verification_result,
    empty_verification_preview as _empty_verification_preview,
    empty_verification_result as _empty_verification_result,
    extract_compare_commit_shas as _extract_compare_commit_shas,
    extract_compare_file_paths as _extract_compare_file_paths,
    normalize_repo as _normalize_repo,
    parse_json_object as _parse_json_object,
)
from agent_taskflow.models import utc_now_iso
from agent_taskflow.pr_handoff_package import (
    PrHandoffPackageRequest,
    create_pr_handoff_package,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


SOURCE = "draft_pr_record"
ACCEPTED_PR_STATES = {"OPEN", "MERGED"}


__all__ = [
    "DraftPrRecordRequest",
    "DraftPrRecordResult",
    "DraftPrConfirmError",
    "record_existing_draft_pr",
    "SOURCE",
]


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class DraftPrRecordRequest:
    """Request for recording draft_pr evidence for an existing PR."""

    task_key: str
    repo: str
    pr_number: int
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    target_repo: str | None = None
    allow_source_repo_mismatch: bool = False
    allow_non_waiting: bool = False
    dry_run: bool = False
    confirm_record_existing_pr: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "repo", _normalize_repo(self.repo))
        if not isinstance(self.pr_number, int) or isinstance(self.pr_number, bool):
            raise TypeError("pr_number must be an int")
        if self.pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
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
        if self.target_repo is not None:
            normalized = str(self.target_repo).strip()
            if not normalized:
                raise ValueError("target_repo must not be empty when provided")
            object.__setattr__(self, "target_repo", _normalize_repo(normalized))
        if not isinstance(self.allow_source_repo_mismatch, bool):
            raise TypeError("allow_source_repo_mismatch must be a bool")


@dataclass(frozen=True)
class DraftPrRecordResult:
    """Structured preview or confirmation result for the record helper."""

    ok: bool
    status: str
    task_key: str
    repo: str
    pr_number: int | None
    pr_url: str | None
    pr_state: str | None
    is_draft: bool | None
    merged: bool
    head_sha: str | None
    base_branch: str | None
    head_branch: str | None
    title: str | None
    body_preview: str | None
    handoff: dict[str, Any]
    verification: dict[str, Any]
    evidence: dict[str, Any]
    artifact_path: str | None
    artifact_recorded: bool
    event_recorded: bool
    safety: dict[str, Any]
    warnings: list[str]
    dry_run: bool
    confirmation_required: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "status": self.status,
            "task_key": self.task_key,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "pr_state": self.pr_state,
            "is_draft": self.is_draft,
            "merged": self.merged,
            "head_sha": self.head_sha,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "title": self.title,
            "body_preview": self.body_preview,
            "handoff": self.handoff,
            "verification": self.verification,
            "evidence": self.evidence,
            "artifact_path": self.artifact_path,
            "artifact_recorded": self.artifact_recorded,
            "event_recorded": self.event_recorded,
            "safety": self.safety,
            "warnings": self.warnings,
            "dry_run": self.dry_run,
            "confirmation_required": self.confirmation_required,
            "error": self.error,
        }
        return json.loads(json.dumps(payload, sort_keys=True))


def record_existing_draft_pr(
    request: DraftPrRecordRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> DraftPrRecordResult:
    """Preview or record draft_pr evidence for an existing GitHub PR."""

    db_path = request.db_path or DEFAULT_DB_PATH
    if not db_path.exists():
        return _error_result(
            request=request,
            status="not_found",
            error=f"SQLite state DB not found: {db_path}",
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
            error=handoff.error or "PR handoff package is not ready",
            warnings=list(handoff.warnings),
            handoff=_handoff_snapshot(handoff),
        )

    task = current_store.get_task(request.task_key)
    if task is None:
        return _error_result(
            request=request,
            status="not_found",
            error=f"Task not found: {request.task_key}",
            handoff=_handoff_snapshot(handoff),
        )
    worktree = current_store.get_task_worktree(request.task_key)
    if worktree is None:
        return _error_result(
            request=request,
            status="blocked",
            error=f"TaskWorktreeRecord missing for task: {request.task_key}",
            handoff=_handoff_snapshot(handoff),
        )

    # Source/target repo mismatch handling (mirrors draft_pr_confirm).
    target_repo = (request.target_repo or request.repo).strip()
    if request.target_repo and target_repo != request.repo:
        return _error_result(
            request=request,
            status="blocked",
            error=(
                f"target_repo {target_repo!r} does not match repo {request.repo!r}; "
                "pass the same value to --repo and --target-repo"
            ),
            handoff=_handoff_snapshot(handoff),
        )
    source_repo = str(handoff.source.get("repo") or "").strip()
    source_repo_overridden = False
    override_warnings: list[str] = []
    if source_repo and source_repo != target_repo:
        if not request.allow_source_repo_mismatch:
            return _error_result(
                request=request,
                status="blocked",
                error=(
                    f"Repo target {target_repo!r} does not match handoff repo "
                    f"{source_repo!r}"
                ),
                handoff=_handoff_snapshot(handoff),
            )
        if request.target_repo is None:
            return _error_result(
                request=request,
                status="blocked",
                error="Source repo mismatch override requires explicit --target-repo",
                handoff=_handoff_snapshot(handoff),
            )
        source_repo_overridden = True
        override_warnings.append(
            "Source repo differs from target repo; override explicitly allowed."
        )

    expected_base = str(handoff.handoff.get("proposed_pr_base") or worktree.base_branch or "").strip()
    expected_head = str(handoff.handoff.get("proposed_pr_head") or worktree.branch or "").strip()
    expected_title = str(handoff.handoff.get("proposed_pr_title") or "").strip()
    expected_body = str(handoff.handoff.get("proposed_pr_body") or "")
    if not expected_base or not expected_head or not expected_title:
        return _error_result(
            request=request,
            status="blocked",
            error="Handoff is missing proposed PR base/head/title",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )
    if expected_head in PROTECTED_HEAD_BRANCHES:
        return _error_result(
            request=request,
            status="blocked",
            error=f"Head branch must not be protected: {expected_head}",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    expected_head_sha = str(worktree.base_sha or "").strip()
    # base_sha in the worktree row is the branch's MERGE BASE on main; we use
    # the local branch HEAD instead for compare's head side.
    local_head_sha = _git_rev_parse_head(request.repo_path / ".worktrees" / request.task_key)
    if not local_head_sha:
        local_head_sha = _git_rev_parse_head(Path(str(worktree.worktree_path)))
    base_sha = expected_head_sha
    if not base_sha:
        return _error_result(
            request=request,
            status="blocked",
            error="Worktree base SHA is unavailable for PR verification",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    expected_files = sorted(handoff.git.get("changed_files", []) or [])

    try:
        view_payload = _fetch_pr_view(
            repo=target_repo,
            pr_number=request.pr_number,
            runner=runner,
        )
    except DraftPrConfirmError as exc:
        return _error_result(
            request=request,
            status="blocked",
            error=str(exc),
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    pr_state = str(view_payload.get("state") or "").strip().upper()
    if pr_state not in ACCEPTED_PR_STATES:
        return _error_result(
            request=request,
            status="blocked",
            error=(
                f"PR #{request.pr_number} state {pr_state!r} is not eligible "
                f"for evidence recording; accepted: {sorted(ACCEPTED_PR_STATES)}"
            ),
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    head_ref_oid = str(view_payload.get("headRefOid") or "").strip()
    base_ref_name = str(view_payload.get("baseRefName") or "").strip()
    head_ref_name = str(view_payload.get("headRefName") or "").strip()
    actual_title = str(view_payload.get("title") or "").strip()
    is_merged = pr_state == "MERGED"

    if base_ref_name != expected_base:
        return _error_result(
            request=request,
            status="blocked",
            error=f"PR baseRefName {base_ref_name!r} does not match expected base {expected_base!r}",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )
    if head_ref_name != expected_head:
        return _error_result(
            request=request,
            status="blocked",
            error=f"PR headRefName {head_ref_name!r} does not match expected head {expected_head!r}",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )
    if actual_title != expected_title:
        return _error_result(
            request=request,
            status="blocked",
            error=f"PR title {actual_title!r} does not match expected title {expected_title!r}",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )
    if local_head_sha and head_ref_oid and head_ref_oid != local_head_sha:
        return _error_result(
            request=request,
            status="blocked",
            error=(
                f"PR headRefOid {head_ref_oid!r} does not match local "
                f"branch HEAD {local_head_sha!r}"
            ),
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    compare_head = head_ref_oid or local_head_sha or expected_head
    try:
        compare_payload = _fetch_compare(
            repo=target_repo,
            base=base_sha,
            head=compare_head,
            runner=runner,
        )
    except DraftPrConfirmError as exc:
        return _error_result(
            request=request,
            status="blocked",
            error=str(exc),
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    actual_files = _extract_compare_file_paths(compare_payload.get("files"))
    actual_commit_shas = _extract_compare_commit_shas(compare_payload.get("commits"))

    # Build a view-shaped payload combining PR metadata with compare results.
    merged_payload = dict(view_payload)
    merged_payload["files"] = [{"path": p} for p in actual_files]
    merged_payload["commits"] = [{"oid": sha} for sha in actual_commit_shas]

    expected_commits = list(actual_commit_shas)  # canonical, taken from compare
    if not expected_commits:
        return _error_result(
            request=request,
            status="blocked",
            error="Compare endpoint returned zero commits between base SHA and PR head",
            handoff=_handoff_snapshot(handoff),
            warnings=override_warnings,
        )

    expected_dict: dict[str, Any] = {
        "expected_repo": target_repo,
        "expected_base": expected_base,
        "expected_head": expected_head,
        "expected_title": expected_title,
        "expected_files": expected_files,
        "expected_commits": expected_commits,
        "expected_state": pr_state,  # accept whichever of OPEN/MERGED we got
        "expected_is_draft": bool(view_payload.get("isDraft")),
    }
    verification = _verification_result(merged_payload, expected=expected_dict)

    blocking_warnings: list[str] = list(verification.get("blocking_warnings", []))

    if not verification.get("passed"):
        return _error_result(
            request=request,
            status="verification_failed",
            error="Verification failed: " + "; ".join(blocking_warnings or ["unknown reason"]),
            handoff=_handoff_snapshot(handoff),
            verification=verification,
            view_payload=view_payload,
            warnings=override_warnings + blocking_warnings,
        )

    target_repo_block = {
        "source_repo": source_repo,
        "target_repo": target_repo,
        "source_repo_overridden": source_repo_overridden,
        "source_repo_mismatch_allowed": bool(request.allow_source_repo_mismatch),
    }

    body_text = expected_body
    body_preview_text = _body_preview(body_text)

    safety = {
        "human_confirmation_required": True,
        "human_confirmation_confirmed": bool(request.confirm_record_existing_pr),
        "task_status_changed": False,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "branch_pushed": False,
        "branch_push_required_before_pr": False,
        "pr_created": False,
        "draft_pr": bool(view_payload.get("isDraft")),
        "draft_pr_verified": True,
        "merged": is_merged,
        "approved": False,
        "cleanup_performed": False,
        "issue_closed": False,
        "branch_deleted": False,
        "worktree_deleted": False,
        "background_worker_started": False,
        "github_mutated": False,
        "read_only_github": True,
        "recorded_post_merge": is_merged,
        "human_review_external": True,
    }
    for key, value in target_repo_block.items():
        safety.setdefault(key, value)

    if request.dry_run or not request.confirm_record_existing_pr:
        evidence_preview = {
            "artifact_recorded": False,
            "event_recorded": False,
            "branch_push_artifact_exists": _branch_push_recorded(current_store, request.task_key),
        }
        evidence_preview.update(target_repo_block)
        status = "dry_run" if request.dry_run else "blocked"
        error = None
        confirmation_required = not request.confirm_record_existing_pr
        if not request.dry_run and not request.confirm_record_existing_pr:
            error = (
                "Recording existing PR evidence requires --confirm-record-existing-pr"
            )
        return DraftPrRecordResult(
            ok=request.dry_run,
            status=status,
            task_key=request.task_key,
            repo=target_repo,
            pr_number=int(view_payload.get("number") or request.pr_number),
            pr_url=str(view_payload.get("url") or ""),
            pr_state=pr_state,
            is_draft=bool(view_payload.get("isDraft")),
            merged=is_merged,
            head_sha=head_ref_oid or None,
            base_branch=base_ref_name or None,
            head_branch=head_ref_name or None,
            title=actual_title or None,
            body_preview=body_preview_text,
            handoff=_handoff_snapshot(handoff),
            verification=verification,
            evidence=evidence_preview,
            artifact_path=None,
            artifact_recorded=False,
            event_recorded=False,
            safety=safety,
            warnings=override_warnings,
            dry_run=request.dry_run,
            confirmation_required=confirmation_required,
            error=error,
        )

    # Confirmed write path.
    artifact_path = _record_artifact_path(request=request, task=task)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_dict = _record_evidence(
        task_key=request.task_key,
        repo=target_repo,
        base=expected_base,
        head=expected_head,
        title=actual_title,
        body=body_text,
        pr_number=int(view_payload.get("number") or request.pr_number),
        pr_url=str(view_payload.get("url") or ""),
        head_sha=head_ref_oid,
        merged=is_merged,
        is_draft=bool(view_payload.get("isDraft")),
        verification=verification,
        created_at=utc_now_iso(),
        target_repo_block=target_repo_block,
    )
    artifact_path.write_text(
        json.dumps(evidence_dict, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, artifact_path)
    current_store.record_task_event(
        request.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Existing draft PR evidence recorded",
        payload=evidence_dict,
    )

    evidence_summary = {
        "artifact_recorded": True,
        "event_recorded": True,
        "branch_push_artifact_exists": _branch_push_recorded(current_store, request.task_key),
    }
    evidence_summary.update(target_repo_block)

    return DraftPrRecordResult(
        ok=True,
        status="recorded",
        task_key=request.task_key,
        repo=target_repo,
        pr_number=int(view_payload.get("number") or request.pr_number),
        pr_url=str(view_payload.get("url") or ""),
        pr_state=pr_state,
        is_draft=bool(view_payload.get("isDraft")),
        merged=is_merged,
        head_sha=head_ref_oid or None,
        base_branch=base_ref_name or None,
        head_branch=head_ref_name or None,
        title=actual_title or None,
        body_preview=body_preview_text,
        handoff=_handoff_snapshot(handoff),
        verification=verification,
        evidence=evidence_summary,
        artifact_path=str(artifact_path),
        artifact_recorded=True,
        event_recorded=True,
        safety=safety,
        warnings=override_warnings,
        dry_run=False,
        confirmation_required=False,
        error=None,
    )


def _git_rev_parse_head(worktree_path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _fetch_pr_view(
    *,
    repo: str,
    pr_number: int,
    runner: Runner | None,
) -> dict[str, Any]:
    completed = _run_command(
        _build_gh_view_command(repo, str(pr_number)),
        runner=runner,
    )
    if completed.returncode != 0:
        raise DraftPrConfirmError(
            f"gh pr view failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    payload = _parse_json_object(completed.stdout, source="gh pr view")
    return payload


def _fetch_compare(
    *,
    repo: str,
    base: str,
    head: str,
    runner: Runner | None,
) -> dict[str, Any]:
    completed = _run_command(
        _build_gh_compare_command(repo=repo, base=base, head=head),
        runner=runner,
    )
    if completed.returncode != 0:
        raise DraftPrConfirmError(
            f"gh api compare failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    return _parse_json_object(completed.stdout, source="gh api compare")


def _run_command(command: list[str], *, runner: Runner | None) -> CompletedProcessLike:
    try:
        return (runner or subprocess.run)(
            command,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:  # pragma: no cover - defensive runtime guard
        raise DraftPrConfirmError(str(exc)) from exc


def _record_artifact_path(*, request: DraftPrRecordRequest, task: Any) -> Path:
    if request.artifact_root is not None:
        return request.artifact_root / "draft_pr" / request.task_key / "draft_pr.json"
    artifact_dir = getattr(task, "artifact_dir", None)
    if artifact_dir:
        return (
            Path(artifact_dir).expanduser().resolve().parent
            / "draft_pr"
            / request.task_key
            / "draft_pr.json"
        )
    return request.repo_path / ".agent-taskflow" / "artifacts" / request.task_key / "draft_pr.json"


def _record_evidence(
    *,
    task_key: str,
    repo: str,
    base: str,
    head: str,
    title: str,
    body: str,
    pr_number: int,
    pr_url: str,
    head_sha: str,
    merged: bool,
    is_draft: bool,
    verification: dict[str, Any],
    created_at: str,
    target_repo_block: dict[str, Any],
) -> dict[str, Any]:
    # The recorded draft_pr.json must match the on-disk shape that
    # confirm_draft_pr would have written at draft-creation time, so the
    # downstream closeout/cleanup chain reads it the same way regardless of
    # whether the PR was created by agent-taskflow or recorded
    # retroactively. The live merge state is fetched separately by closeout
    # / cleanup tooling and is not encoded in this artifact's ``merged``
    # field. ``current_state`` / ``recorded_post_merge`` /
    # ``human_review_external`` carry the observed retro-record context.
    safety = {
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
        "github_mutated": False,
        "read_only_github": True,
        "recorded_post_merge": merged,
        "human_review_external": True,
    }
    for key, value in target_repo_block.items():
        safety.setdefault(key, value)
    evidence = {
        "kind": EVENT_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": task_key,
        "repo": repo,
        "base_branch": base,
        "head_branch": head,
        "head_sha": head_sha,
        "title": title,
        "body": body,
        "body_path": None,
        "body_preview": _body_preview(body),
        "draft": True,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "branch_push_verified": True,
        "verified": True,
        "verification": verification,
        "branch_push_artifact_path": None,
        "branch_push_event_type": "branch_push_completed",
        "created_at": created_at,
        "pr_created": True,
        "draft_pr_created": True,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "issue_closed": False,
        "requires_human_confirmation": True,
        "human_review_external": True,
        "recorded_post_merge": merged,
        "current_state": "MERGED" if merged else ("OPEN" if is_draft else "OPEN"),
        "current_is_draft": is_draft,
        "safety": safety,
    }
    evidence.update(target_repo_block)
    return evidence


def _branch_push_recorded(store: TaskMirrorStore, task_key: str) -> bool:
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type == BRANCH_PUSH_ARTIFACT_TYPE:
            return True
    return False


def _handoff_snapshot(handoff: Any) -> dict[str, Any]:
    if handoff is None:
        return {}
    return {
        "ready_for_draft_pr_review": bool(handoff.summary.get("ready_for_draft_pr_review")),
        "ready_for_branch_push_review": bool(handoff.summary.get("ready_for_branch_push_review")),
        "source_repo": str(handoff.source.get("repo") or "").strip(),
        "proposed_pr_title": handoff.handoff.get("proposed_pr_title"),
        "proposed_pr_base": handoff.handoff.get("proposed_pr_base"),
        "proposed_pr_head": handoff.handoff.get("proposed_pr_head"),
        "expected_files": list(handoff.git.get("changed_files", []) or []),
    }


def _error_result(
    *,
    request: DraftPrRecordRequest,
    status: str,
    error: str,
    handoff: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    view_payload: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> DraftPrRecordResult:
    target_repo = (request.target_repo or request.repo).strip()
    safety = {
        "human_confirmation_required": True,
        "human_confirmation_confirmed": False,
        "task_status_changed": False,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "branch_pushed": False,
        "branch_push_required_before_pr": False,
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
        "github_mutated": False,
        "read_only_github": True,
    }
    block = {
        "source_repo": "",
        "target_repo": target_repo,
        "source_repo_overridden": False,
        "source_repo_mismatch_allowed": bool(request.allow_source_repo_mismatch),
    }
    if handoff is not None:
        block["source_repo"] = str(handoff.get("source_repo") or "").strip()
    safety.update(block)
    return DraftPrRecordResult(
        ok=False,
        status=status,
        task_key=request.task_key,
        repo=target_repo,
        pr_number=int(request.pr_number) if request.pr_number else None,
        pr_url=str(view_payload.get("url")) if view_payload and view_payload.get("url") else None,
        pr_state=str(view_payload.get("state")) if view_payload and view_payload.get("state") else None,
        is_draft=bool(view_payload.get("isDraft")) if view_payload and view_payload.get("isDraft") is not None else None,
        merged=bool(view_payload.get("state") == "MERGED") if view_payload else False,
        head_sha=str(view_payload.get("headRefOid")) if view_payload and view_payload.get("headRefOid") else None,
        base_branch=str(view_payload.get("baseRefName")) if view_payload and view_payload.get("baseRefName") else None,
        head_branch=str(view_payload.get("headRefName")) if view_payload and view_payload.get("headRefName") else None,
        title=str(view_payload.get("title")) if view_payload and view_payload.get("title") else None,
        body_preview=None,
        handoff=handoff or {},
        verification=verification or _empty_verification_result(expected=_empty_verification_preview()),
        evidence=dict(block, artifact_recorded=False, event_recorded=False),
        artifact_path=None,
        artifact_recorded=False,
        event_recorded=False,
        safety=safety,
        warnings=list(warnings or []),
        dry_run=request.dry_run,
        confirmation_required=not request.confirm_record_existing_pr,
        error=error,
    )
