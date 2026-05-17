"""Explicit draft PR creation from existing local handoff evidence.

This module is intentionally narrow. It can create a GitHub draft PR only when
called with an explicit confirmation and a valid immutable PR handoff package.
It never pushes, merges, approves, cleans up, deletes branches or worktrees, or
mutates GitHub Issues/Projects.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


EVENT_TYPE = "draft_pr_created"
ARTIFACT_TYPE = "draft_pr"
SOURCE = "draft_pr"


class DraftPrError(RuntimeError):
    """Raised when a draft PR cannot be safely created."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class DraftPrCreationRequest:
    """Request for creating or previewing a draft PR from handoff evidence."""

    task_key: str
    db_path: Path | None = None
    repo: str | None = None
    handoff_json: Path | None = None
    dry_run: bool = True
    confirm_create_pr: bool = False
    base_branch: str | None = None
    head_branch: str | None = None
    title: str | None = None
    body: str | None = None
    draft_only: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                ensure_absolute_path(self.db_path, name="db_path"),
            )
        if self.handoff_json is not None:
            object.__setattr__(
                self,
                "handoff_json",
                ensure_absolute_path(self.handoff_json, name="handoff_json"),
            )


@dataclass(frozen=True)
class DraftPrCommandPreview:
    """Inert command preview for human/operator inspection."""

    argv: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass(frozen=True)
class DraftPrCreationResult:
    """Result of a dry-run or confirmed draft PR creation."""

    ok: bool
    status: str
    task_key: str
    repo: str
    pr_url: str | None
    pr_number: int | None
    is_draft: bool | None
    base_branch: str
    head_branch: str
    title: str
    command_preview: str
    github_mutated: bool
    pr_created: bool
    pushed: bool
    merged: bool
    cleanup_performed: bool
    event_recorded: bool
    artifact_recorded: bool
    dry_run: bool
    confirmation_required: bool
    summary: str
    draft_pr_json_path: Path | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "task_key": self.task_key,
            "repo": self.repo,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "is_draft": self.is_draft,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "title": self.title,
            "command_preview": self.command_preview,
            "github_mutated": self.github_mutated,
            "pr_created": self.pr_created,
            "pushed": self.pushed,
            "merged": self.merged,
            "cleanup_performed": self.cleanup_performed,
            "event_recorded": self.event_recorded,
            "artifact_recorded": self.artifact_recorded,
            "evidence_recorded": self.event_recorded and self.artifact_recorded,
            "dry_run": self.dry_run,
            "confirmation_required": self.confirmation_required,
            "draft_pr_json_path": str(self.draft_pr_json_path)
            if self.draft_pr_json_path
            else None,
            "summary": self.summary,
        }


def create_draft_pr(
    request: DraftPrCreationRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> DraftPrCreationResult:
    """Preview or create a GitHub draft PR from a valid handoff package."""

    if not request.draft_only:
        raise DraftPrError("Only draft PR creation is supported")

    current_store = store or TaskMirrorStore(request.db_path)
    current_store.init_db()

    context = _load_context(current_store, request)
    command = _build_gh_command(context)
    preview = DraftPrCommandPreview(tuple(command)).text
    should_create = request.confirm_create_pr and not request.dry_run

    if not should_create:
        return DraftPrCreationResult(
            ok=True,
            status="dry_run",
            task_key=request.task_key,
            repo=context["repo"],
            pr_url=None,
            pr_number=None,
            is_draft=None,
            base_branch=context["base_branch"],
            head_branch=context["head_branch"],
            title=context["title"],
            command_preview=preview,
            github_mutated=False,
            pr_created=False,
            pushed=False,
            merged=False,
            cleanup_performed=False,
            event_recorded=False,
            artifact_recorded=False,
            dry_run=True,
            confirmation_required=not request.confirm_create_pr,
            summary="Dry run only; no GitHub mutation performed",
            draft_pr_json_path=context["draft_pr_json_path"],
        )

    completed = (runner or subprocess.run)(
        command,
        cwd=context["worktree_path"],
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise DraftPrError(
            "gh pr create failed with "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )

    gh_payload = _parse_gh_output(completed.stdout)
    if gh_payload.get("isDraft") is not True:
        raise DraftPrError("GitHub response did not confirm a draft PR")

    pr_url = _require_non_empty_str(gh_payload, "url")
    pr_number = gh_payload.get("number")
    if not isinstance(pr_number, int):
        raise DraftPrError("GitHub response missing numeric PR number")

    artifact_path = context["draft_pr_json_path"]
    evidence = _draft_pr_evidence(
        task_key=request.task_key,
        repo=context["repo"],
        pr_url=pr_url,
        pr_number=pr_number,
        is_draft=True,
        base_branch=context["base_branch"],
        head_branch=context["head_branch"],
        title=context["title"],
        command_preview=preview,
        handoff_json_path=context["handoff_json_path"],
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, artifact_path)
    current_store.record_task_event(
        request.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Draft PR created",
        payload=evidence,
    )

    return DraftPrCreationResult(
        ok=True,
        status="created",
        task_key=request.task_key,
        repo=context["repo"],
        pr_url=pr_url,
        pr_number=pr_number,
        is_draft=True,
        base_branch=context["base_branch"],
        head_branch=context["head_branch"],
        title=context["title"],
        command_preview=preview,
        github_mutated=True,
        pr_created=True,
        pushed=False,
        merged=False,
        cleanup_performed=False,
        event_recorded=True,
        artifact_recorded=True,
        dry_run=False,
        confirmation_required=False,
        summary="Draft PR created",
        draft_pr_json_path=artifact_path,
    )


def _load_context(
    store: TaskMirrorStore,
    request: DraftPrCreationRequest,
) -> dict[str, Any]:
    task = store.get_task(request.task_key)
    if task is None:
        raise DraftPrError(f"Task not found: {request.task_key}")
    if task.status != "waiting_approval":
        raise DraftPrError(
            f"Task {task.task_key} must be waiting_approval, got {task.status}"
        )

    worktree = store.get_task_worktree(task.task_key)
    if worktree is None:
        raise DraftPrError(f"TaskWorktreeRecord missing for task: {task.task_key}")
    if not worktree.worktree_path.is_dir():
        raise DraftPrError(f"Worktree path is missing: {worktree.worktree_path}")

    handoff_json_path = request.handoff_json or _find_handoff_json(store, task.task_key)
    if not handoff_json_path.is_file():
        raise DraftPrError(f"pr_handoff.json is missing: {handoff_json_path}")

    handoff = _load_handoff(handoff_json_path)
    _validate_handoff(handoff, task_key=task.task_key)

    proposed_pr = handoff["proposed_pr"]
    repo = request.repo or handoff.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        raise DraftPrError("--repo is required when pr_handoff.json has no repo")

    base_branch = request.base_branch or proposed_pr.get("base_branch")
    head_branch = request.head_branch or proposed_pr.get("head_branch")
    title = request.title or proposed_pr.get("title")
    body = request.body or proposed_pr.get("body")
    for field_name, value in {
        "base_branch": base_branch,
        "head_branch": head_branch,
        "title": title,
        "body": body,
    }.items():
        if not isinstance(value, str) or not value.strip():
            raise DraftPrError(f"{field_name} is required")

    if head_branch != worktree.branch:
        raise DraftPrError(
            f"handoff head branch {head_branch!r} does not match worktree branch "
            f"{worktree.branch!r}"
        )

    return {
        "task": task,
        "worktree": worktree,
        "repo": repo.strip(),
        "base_branch": base_branch.strip(),
        "head_branch": head_branch.strip(),
        "title": title.strip(),
        "body": body,
        "worktree_path": worktree.worktree_path,
        "handoff_json_path": handoff_json_path,
        "draft_pr_json_path": handoff_json_path.parent / "draft_pr.json",
    }


def _find_handoff_json(store: TaskMirrorStore, task_key: str) -> Path:
    candidates = [
        artifact.path
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == "pr_handoff"
    ]
    if not candidates:
        raise DraftPrError(f"pr_handoff artifact missing for task: {task_key}")
    return candidates[-1]


def _load_handoff(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DraftPrError(f"Invalid pr_handoff.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise DraftPrError("pr_handoff.json must contain an object")
    return payload


def _validate_handoff(handoff: dict[str, Any], *, task_key: str) -> None:
    expected_fields = {
        "artifact_type": "pr_handoff",
        "task_key": task_key,
        "task_status": "waiting_approval",
    }
    for field_name, expected in expected_fields.items():
        if handoff.get(field_name) != expected:
            raise DraftPrError(
                f"pr_handoff.json {field_name} must be {expected!r}"
            )

    proposed_pr = handoff.get("proposed_pr")
    if not isinstance(proposed_pr, dict):
        raise DraftPrError("pr_handoff.json proposed_pr is required")
    if proposed_pr.get("draft_recommended") is not True:
        raise DraftPrError("pr_handoff.json must recommend a draft PR")
    for field_name in ("base_branch", "head_branch", "title", "body"):
        value = proposed_pr.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise DraftPrError(f"pr_handoff.json proposed_pr.{field_name} is required")

    safety = handoff.get("safety")
    if not isinstance(safety, dict):
        raise DraftPrError("pr_handoff.json safety is required")
    expected_safety = {
        "pr_created": False,
        "pushed": False,
        "merged": False,
        "cleanup_performed": False,
        "github_mutated": False,
        "human_review_required": True,
    }
    for field_name, expected in expected_safety.items():
        if safety.get(field_name) is not expected:
            raise DraftPrError(
                f"pr_handoff.json safety.{field_name} must be {expected}"
            )


def _build_gh_command(context: dict[str, Any]) -> list[str]:
    return [
        "gh",
        "pr",
        "create",
        "--draft",
        "--repo",
        context["repo"],
        "--base",
        context["base_branch"],
        "--head",
        context["head_branch"],
        "--title",
        context["title"],
        "--body",
        context["body"],
        "--json",
        "url,number,headRefName,baseRefName,isDraft",
    ]


def _parse_gh_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DraftPrError(f"gh pr create returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DraftPrError("gh pr create returned non-object JSON")
    return payload


def _require_non_empty_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise DraftPrError(f"GitHub response missing {field_name}")
    return value.strip()


def _draft_pr_evidence(
    *,
    task_key: str,
    repo: str,
    pr_url: str,
    pr_number: int,
    is_draft: bool,
    base_branch: str,
    head_branch: str,
    title: str,
    command_preview: str,
    handoff_json_path: Path,
) -> dict[str, Any]:
    return {
        "kind": EVENT_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": task_key,
        "repo": repo,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "is_draft": is_draft,
        "base_branch": base_branch,
        "head_branch": head_branch,
        "title": title,
        "command_preview": command_preview,
        "handoff_json_path": str(handoff_json_path),
        "safety": {
            "pr_created": True,
            "pushed": False,
            "merged": False,
            "cleanup_performed": False,
            "human_review_required": True,
        },
        "generated_at": utc_now_iso(),
    }
