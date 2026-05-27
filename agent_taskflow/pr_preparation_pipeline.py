"""Level 7C PR preparation after waiting_approval.

This module is an explicit, single-task operator command that turns an
already-executed ``waiting_approval`` task into local PR handoff evidence,
a pushed task branch, and a GitHub draft PR. It composes the existing PR
handoff, branch push confirmation, and draft PR confirmation helpers.

It does not ingest issues, run runtime execution, invoke approved task
runner, run executors or validators, approve, merge, clean up, start loops,
or expose API/Mission Control actions.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.branch_push_confirm import (
    ARTIFACT_TYPE as BRANCH_PUSH_ARTIFACT_TYPE,
    BranchPushConfirmRequest,
    EVENT_TYPE as BRANCH_PUSH_EVENT_TYPE,
    confirm_branch_push,
)
from agent_taskflow.draft_pr_confirm import (
    ARTIFACT_TYPE as DRAFT_PR_ARTIFACT_TYPE,
    DraftPrConfirmRequest,
    EVENT_TYPE as DRAFT_PR_EVENT_TYPE,
    confirm_draft_pr,
)
from agent_taskflow.pr_handoff import (
    ARTIFACT_TYPE as PR_HANDOFF_ARTIFACT_TYPE,
    EVENT_TYPE as PR_HANDOFF_EVENT_TYPE,
    PrHandoffRequest,
    create_pr_handoff,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.waiting_approval_summary import (
    WaitingApprovalSummaryRequest,
    summarize_waiting_approval_task,
)


PR_PREPARATION_PIPELINE_SCHEMA_VERSION = "pr_preparation_pipeline.v1"
PR_PREPARATION_PIPELINE_SOURCE = "pr_preparation_pipeline"

PR_PREPARATION_PIPELINE_SAFETY_FLAGS: dict[str, bool] = {
    "one_task_only": True,
    "operator_triggered": True,
    "resume_existing": False,
    "github_mutated": False,
    "branch_pushed": False,
    "draft_pr_created": False,
    "duplicate_draft_pr_created": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "scheduler_loop_started": False,
    "background_worker_started": False,
    "automatic_task_picking_started": False,
    "runtime_execution_started": False,
    "approved_task_runner_called": False,
    "executor_started": False,
    "validators_started": False,
    "human_review_required": True,
}

_STAGE_PREFLIGHT = "preflight"
_STAGE_PR_HANDOFF = "pr_handoff"
_STAGE_BRANCH_PUSH = "branch_push"
_STAGE_DRAFT_PR = "draft_pr"


class PRPreparationPipelineError(RuntimeError):
    """Raised when Level 7C cannot proceed without violating its contract."""


@dataclass(frozen=True)
class PRPreparationPipelineRequest:
    """Inputs for Level 7C PR preparation."""

    db_path: Path
    artifact_root: Path
    task_key: str
    dry_run: bool = True
    confirm_prepare_pr: bool = False
    confirm_github_mutations: bool = False
    confirm_branch_push: bool = False
    confirm_draft_pr: bool = False
    resume_existing: bool = False
    allow_repush: bool = False
    operator: str | None = None
    operator_note: str | None = None
    remote: str = "origin"
    base_branch: str | None = None
    draft: bool = True

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        artifact_root = Path(self.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        object.__setattr__(self, "artifact_root", artifact_root)

        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        for field_name in ("operator", "operator_note", "base_branch"):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = value.strip()
            object.__setattr__(self, field_name, stripped or None)

        remote = self.remote.strip()
        if not remote:
            raise ValueError("remote must not be empty")
        object.__setattr__(self, "remote", remote)


def run_pr_preparation_pipeline(
    request: PRPreparationPipelineRequest,
    *,
    branch_push_fn: Callable[..., dict[str, Any]] | None = None,
    draft_pr_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run Level 7C for one waiting-approval task.

    Dry-run mode validates local readiness and returns a no-write preview.
    Confirmed mode requires all mutation confirmations before any handoff
    evidence is written or any GitHub mutation helper is called.
    """

    if not request.draft:
        raise PRPreparationPipelineError(
            "Level 7C supports draft PR creation only"
        )

    if not request.dry_run:
        _require_all_confirmations(request)

    preflight = _run_preflight(request)
    stages: dict[str, Any] = {_STAGE_PREFLIGHT: preflight["summary"]}
    if not preflight["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_PREFLIGHT,
            reasons=preflight["reasons"],
            stage_result=preflight,
            stages=stages,
        )

    if request.dry_run:
        handoff_preview = _run_pr_handoff_stage(
            request,
            preflight=preflight,
            dry_run=True,
        )
        stages[_STAGE_PR_HANDOFF] = handoff_preview["summary"]
        if not handoff_preview["ok"]:
            return _failure_response(
                request,
                failed_stage=_STAGE_PR_HANDOFF,
                reasons=handoff_preview["reasons"],
                stage_result=handoff_preview,
                stages=stages,
            )
        return _dry_run_response(request, preflight=preflight, handoff=handoff_preview)

    handoff_stage = _run_pr_handoff_stage(
        request,
        preflight=preflight,
        dry_run=False,
    )
    stages[_STAGE_PR_HANDOFF] = handoff_stage["summary"]
    if not handoff_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_PR_HANDOFF,
            reasons=handoff_stage["reasons"],
            stage_result=handoff_stage,
            stages=stages,
        )

    branch_stage = _run_branch_push_stage(
        request,
        preflight=preflight,
        handoff=handoff_stage,
        branch_push_fn=branch_push_fn or _default_branch_push_fn,
    )
    stages[_STAGE_BRANCH_PUSH] = branch_stage["summary"]
    if not branch_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_BRANCH_PUSH,
            reasons=branch_stage["reasons"],
            stage_result=branch_stage,
            stages=stages,
            branch_pushed=branch_stage["summary"].get("pushed") is True,
        )

    draft_stage = _run_draft_pr_stage(
        request,
        preflight=preflight,
        branch_push=branch_stage,
        draft_pr_fn=draft_pr_fn or _default_draft_pr_fn,
    )
    stages[_STAGE_DRAFT_PR] = draft_stage["summary"]
    if not draft_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_DRAFT_PR,
            reasons=draft_stage["reasons"],
            stage_result=draft_stage,
            stages=stages,
            branch_pushed=branch_stage["summary"].get("pushed") is True,
            draft_pr_created=draft_stage["summary"].get("created") is True,
        )

    branch_pushed = branch_stage["summary"].get("pushed") is True
    draft_pr_created = draft_stage["summary"].get("created") is True
    draft_pr_already_created = draft_stage["summary"].get("already_created") is True
    return {
        "ok": True,
        "schema_version": PR_PREPARATION_PIPELINE_SCHEMA_VERSION,
        "source": PR_PREPARATION_PIPELINE_SOURCE,
        "status": (
            "draft_pr_already_created"
            if draft_pr_already_created
            else "draft_pr_created"
        ),
        "mode": "confirmed",
        "task_key": request.task_key,
        "stages": stages,
        "safety": _safety(
            dry_run=False,
            resume_existing=request.resume_existing,
            github_mutated=branch_pushed or draft_pr_created,
            branch_pushed=branch_pushed,
            draft_pr_created=draft_pr_created,
        ),
    }


def _require_all_confirmations(request: PRPreparationPipelineRequest) -> None:
    missing: list[str] = []
    if not request.confirm_prepare_pr:
        missing.append("--confirm-prepare-pr")
    if not request.confirm_github_mutations:
        missing.append("--confirm-github-mutations")
    if not request.confirm_branch_push:
        missing.append("--confirm-branch-push")
    if not request.confirm_draft_pr:
        missing.append("--confirm-draft-pr")
    if missing:
        raise PRPreparationPipelineError(
            "Confirmed PR preparation requires all GitHub mutation "
            f"confirmations before any write or mutation: {', '.join(missing)}"
        )


def _run_preflight(request: PRPreparationPipelineRequest) -> dict[str, Any]:
    reasons: list[str] = []
    if not request.db_path.exists():
        reasons.append("state_db_missing")
        return _preflight_result(
            request,
            reasons=reasons,
            task_status=None,
            worktree=None,
            runtime=None,
            repo=None,
        )

    store = TaskMirrorStore(request.db_path)
    try:
        task = store.get_task(request.task_key)
    except Exception as exc:
        reasons.append(f"state_db_read_error: {exc.__class__.__name__}: {exc}")
        return _preflight_result(
            request,
            reasons=reasons,
            task_status=None,
            worktree=None,
            runtime=None,
            repo=None,
        )

    if task is None:
        reasons.append("task_missing")
        return _preflight_result(
            request,
            reasons=reasons,
            task_status=None,
            worktree=None,
            runtime=None,
            repo=None,
        )

    if task.status != "waiting_approval":
        reasons.append(f"task_status_not_waiting_approval: {task.status}")

    worktree = store.get_task_worktree(request.task_key)
    if worktree is None:
        reasons.append("task_worktree_missing")
    else:
        if not str(worktree.branch or "").strip():
            reasons.append("task_worktree_branch_missing")
        if not str(worktree.base_branch or "").strip():
            reasons.append("task_worktree_base_branch_missing")
        if not str(worktree.base_sha or "").strip():
            reasons.append("task_worktree_base_sha_missing")
        if request.base_branch is not None and worktree.base_branch != request.base_branch:
            reasons.append(
                "requested_base_branch_mismatch: "
                f"{request.base_branch} != {worktree.base_branch}"
            )

    runtime = _runtime_evidence(store, request.task_key)
    reasons.extend(runtime["reasons"])

    repo = _source_repo(request)
    if repo is None:
        reasons.append("source_repo_missing")

    return _preflight_result(
        request,
        reasons=_unique_strings(reasons),
        task_status=task.status,
        worktree=worktree,
        runtime=runtime,
        repo=repo,
    )


def _preflight_result(
    request: PRPreparationPipelineRequest,
    *,
    reasons: list[str],
    task_status: str | None,
    worktree: Any,
    runtime: dict[str, Any] | None,
    repo: str | None,
) -> dict[str, Any]:
    passed = not reasons
    runtime = runtime or {
        "artifact_count": 0,
        "finished_event_count": 0,
        "runtime_evidence_found": False,
        "runner_ok": None,
    }
    summary = {
        "passed": passed,
        "task_status": task_status,
        "runtime_evidence_found": bool(runtime.get("runtime_evidence_found")),
        "runtime_artifact_count": runtime.get("artifact_count", 0),
        "runtime_finished_event_count": runtime.get("finished_event_count", 0),
        "runner_ok": runtime.get("runner_ok"),
        "worktree_found": worktree is not None,
        "worktree_path": str(worktree.worktree_path) if worktree is not None else None,
        "branch": worktree.branch if worktree is not None else None,
        "base_branch": worktree.base_branch if worktree is not None else request.base_branch,
        "repo": repo,
    }
    return {
        "ok": passed,
        "stage": _STAGE_PREFLIGHT,
        "summary": summary,
        "reasons": list(reasons),
        "repo": repo,
        "worktree": {
            "repo_path": str(worktree.repo_path) if worktree is not None else None,
            "worktree_path": str(worktree.worktree_path) if worktree is not None else None,
            "branch": worktree.branch if worktree is not None else None,
            "base_branch": worktree.base_branch if worktree is not None else None,
            "base_sha": worktree.base_sha if worktree is not None else None,
        },
    }


def _runtime_evidence(store: TaskMirrorStore, task_key: str) -> dict[str, Any]:
    reasons: list[str] = []
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == RUNTIME_EXECUTION_ARTIFACT_TYPE
    ]
    events = [
        event
        for event in store.list_task_events(task_key)
        if event.event_type == RUNTIME_FINISHED_EVENT_TYPE
    ]

    if not artifacts:
        reasons.append("runtime_handoff_execution_artifact_missing")
    if not events:
        reasons.append("runtime_execution_finished_event_missing")

    payloads: list[dict[str, Any]] = []
    for artifact in artifacts:
        payload, artifact_reasons = _read_json_artifact(artifact.path)
        reasons.extend(artifact_reasons)
        if payload is not None:
            payloads.append(payload)
    for event in events:
        payload = _event_payload(event.payload_json)
        if payload is not None:
            payloads.append(payload)

    runner_ok_values = [
        payload.get("runner_ok")
        for payload in payloads
        if isinstance(payload.get("runner_ok"), bool)
    ]
    if any(value is False for value in runner_ok_values):
        reasons.append("runtime_runner_not_ok")

    return {
        "runtime_evidence_found": bool(artifacts and events and not reasons),
        "artifact_count": len(artifacts),
        "finished_event_count": len(events),
        "runner_ok": runner_ok_values[-1] if runner_ok_values else None,
        "reasons": _unique_strings(reasons),
    }


def _read_json_artifact(
    path: Path,
    *,
    context: str = "runtime_handoff_execution_artifact",
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"{context}_file_missing: {path}"]
    except OSError as exc:
        return None, [f"{context}_read_error: {exc}"]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, [f"{context}_json_malformed: {path}"]
    if not isinstance(payload, dict):
        return None, [f"{context}_json_not_object: {path}"]
    return payload, []


def _event_payload(payload_json: str | None) -> dict[str, Any] | None:
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _source_repo(request: PRPreparationPipelineRequest) -> str | None:
    try:
        summary = summarize_waiting_approval_task(
            WaitingApprovalSummaryRequest(
                task_key=request.task_key,
                db_path=request.db_path,
                artifact_root=request.artifact_root,
            )
        )
    except Exception:
        return None
    repo = str((summary.source or {}).get("repo") or "").strip()
    return repo or None


def _run_pr_handoff_stage(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    if request.resume_existing and not dry_run:
        reusable = _find_reusable_pr_handoff(request, preflight=preflight)
        if reusable["state"] == "usable":
            return reusable["stage"]
        if reusable["state"] != "none":
            return _stage_failure(_STAGE_PR_HANDOFF, reusable["reasons"], reusable)

    handoff_request = PrHandoffRequest(
        task_key=request.task_key,
        db_path=request.db_path,
        output_dir=request.artifact_root / "pr_handoff",
        repo=_source_repo(request),
        base_branch=request.base_branch,
        dry_run=dry_run,
    )
    try:
        result = create_pr_handoff(handoff_request)
    except Exception as exc:
        return _stage_failure(
            _STAGE_PR_HANDOFF,
            [f"pr_handoff_error: {exc.__class__.__name__}: {exc}"],
            None,
        )

    payload = result.to_summary_dict()
    summary = {
        "created": bool(result.ok and not dry_run),
        "reused": False,
        "would_create": bool(result.ok and dry_run),
        "artifact_path": str(result.json_path),
        "markdown_path": str(result.markdown_path),
        "artifact_recorded": result.artifact_recorded,
        "event_recorded": result.event_recorded,
        "branch": (payload.get("package") or {}).get("branch"),
        "base_branch": (payload.get("package") or {}).get("base_branch"),
        "head_sha": (payload.get("package") or {}).get("head_sha"),
    }
    return {
        "ok": bool(result.ok),
        "stage": _STAGE_PR_HANDOFF,
        "summary": summary,
        "payload": payload,
        "reasons": [] if result.ok else ["pr_handoff_not_ok"],
    }


def _run_branch_push_stage(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    handoff: dict[str, Any],
    branch_push_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    worktree = preflight["worktree"]
    if request.resume_existing:
        reusable = _find_reusable_branch_push(
            request,
            preflight=preflight,
            handoff=handoff,
        )
        if reusable["state"] == "usable":
            return reusable["stage"]
        if reusable["state"] != "none":
            return _stage_failure(_STAGE_BRANCH_PUSH, reusable["reasons"], reusable)

    try:
        result = _as_dict(
            branch_push_fn(
                task_key=request.task_key,
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                repo_path=Path(str(worktree["worktree_path"])),
                remote=request.remote,
                branch=worktree["branch"],
                dry_run=False,
                confirm_branch_push=True,
                operator=request.operator,
                operator_note=request.operator_note,
            )
        )
    except Exception as exc:
        return _stage_failure(
            _STAGE_BRANCH_PUSH,
            [f"branch_push_error: {exc.__class__.__name__}: {exc}"],
            None,
        )

    pushed = bool(
        result.get("branch_pushed")
        or result.get("push_ok")
        or (result.get("summary") or {}).get("branch_pushed")
    )
    if not result.get("ok") or not pushed:
        return _stage_failure(
            _STAGE_BRANCH_PUSH,
            list(result.get("reasons") or result.get("warnings") or [result.get("error") or "branch_push_not_ok"]),
            result,
        )

    return {
        "ok": True,
        "stage": _STAGE_BRANCH_PUSH,
        "summary": {
            "pushed": True,
            "reused": False,
            "already_pushed": False,
            "remote": result.get("remote") or request.remote,
            "branch": result.get("branch") or worktree["branch"],
            "artifact_path": result.get("branch_push_json_path")
            or (result.get("evidence") or {}).get("artifact_path")
            or result.get("artifact_path"),
        },
        "payload": result,
        "reasons": [],
    }


def _run_draft_pr_stage(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    branch_push: dict[str, Any],
    draft_pr_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    worktree = preflight["worktree"]
    repo = preflight["repo"]
    if request.resume_existing:
        reusable = _find_reusable_draft_pr(
            request,
            preflight=preflight,
            branch_push=branch_push,
        )
        if reusable["state"] == "usable":
            return reusable["stage"]
        if reusable["state"] != "none":
            return _stage_failure(_STAGE_DRAFT_PR, reusable["reasons"], reusable)

    try:
        result = _as_dict(
            draft_pr_fn(
                task_key=request.task_key,
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                repo_path=Path(str(worktree["worktree_path"])),
                repo=repo,
                base=request.base_branch or worktree["base_branch"],
                head=worktree["branch"],
                draft=request.draft,
                dry_run=False,
                confirm_draft_pr=True,
                operator=request.operator,
                operator_note=request.operator_note,
            )
        )
    except Exception as exc:
        return _stage_failure(
            _STAGE_DRAFT_PR,
            [f"draft_pr_error: {exc.__class__.__name__}: {exc}"],
            None,
        )

    draft = result.get("draft_pr") if isinstance(result.get("draft_pr"), dict) else {}
    created = bool(
        draft.get("created")
        or result.get("draft_pr_created")
        or (result.get("summary") or {}).get("draft_pr_created")
    )
    already_created = bool(result.get("status") == "already_exists_verified")
    draft_flag = bool(draft.get("draft", result.get("draft", True)))
    if not result.get("ok") or not (created or already_created) or not draft_flag:
        return _stage_failure(
            _STAGE_DRAFT_PR,
            list(result.get("reasons") or result.get("warnings") or [result.get("error") or "draft_pr_not_created"]),
            result,
        )

    return {
        "ok": True,
        "stage": _STAGE_DRAFT_PR,
        "summary": {
            "created": created,
            "reused": already_created,
            "already_created": already_created,
            "draft": True,
            "pr_url": draft.get("url") or result.get("pr_url"),
            "pr_number": draft.get("number") or result.get("pr_number"),
            "artifact_path": draft.get("artifact_path")
            or (result.get("evidence") or {}).get("artifact_path")
            or result.get("artifact_path"),
        },
        "payload": result,
        "reasons": [],
    }


def _find_reusable_pr_handoff(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    store = TaskMirrorStore(request.db_path)
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(request.task_key)
        if artifact.artifact_type == PR_HANDOFF_ARTIFACT_TYPE
    ]
    events = [
        event
        for event in store.list_task_events(request.task_key)
        if event.event_type == PR_HANDOFF_EVENT_TYPE
    ]
    if not artifacts and not events:
        return _reuse_scan("none", _STAGE_PR_HANDOFF)

    reasons: list[str] = []
    if not artifacts:
        reasons.append("pr_handoff_artifact_missing")
    if not events:
        reasons.append("pr_handoff_event_missing")

    event_paths: set[str] = set()
    event_markdown_paths: dict[str, str] = {}
    for event in events:
        payload, payload_reasons = _read_event_payload(
            event.payload_json,
            context="pr_handoff_event",
        )
        reasons.extend(payload_reasons)
        if payload is None:
            continue
        json_path = str(payload.get("json_path") or "").strip()
        markdown_path = str(payload.get("markdown_path") or "").strip()
        if json_path:
            event_paths.add(json_path)
            if markdown_path:
                event_markdown_paths[json_path] = markdown_path

    valid_candidates: list[dict[str, Any]] = []
    artifact_paths = {str(artifact.path) for artifact in artifacts}
    for artifact in artifacts:
        payload, artifact_reasons = _read_json_artifact(
            artifact.path,
            context="pr_handoff_artifact",
        )
        reasons.extend(artifact_reasons)
        if payload is None:
            continue
        candidate_reasons = _validate_pr_handoff_payload(
            payload,
            request=request,
            preflight=preflight,
        )
        if candidate_reasons:
            reasons.extend(
                f"pr_handoff_invalid:{artifact.path}: {reason}"
                for reason in candidate_reasons
            )
            continue
        valid_candidates.append(
            {
                "artifact_path": str(artifact.path),
                "markdown_path": event_markdown_paths.get(str(artifact.path)),
                "payload": payload,
            }
        )

    for event_path in sorted(event_paths):
        if event_path not in artifact_paths:
            reasons.append(f"pr_handoff_event_artifact_path_unmatched: {event_path}")

    if reasons:
        return _reuse_scan("invalid", _STAGE_PR_HANDOFF, reasons=reasons)

    unique_paths = {candidate["artifact_path"] for candidate in valid_candidates}
    if len(unique_paths) != 1:
        return _reuse_scan(
            "ambiguous",
            _STAGE_PR_HANDOFF,
            reasons=["pr_handoff_evidence_ambiguous"],
        )

    candidate = valid_candidates[0]
    payload = candidate["payload"]
    return _reuse_scan(
        "usable",
        _STAGE_PR_HANDOFF,
        stage_result={
            "ok": True,
            "stage": _STAGE_PR_HANDOFF,
            "summary": {
                "created": False,
                "reused": True,
                "would_create": False,
                "artifact_path": candidate["artifact_path"],
                "markdown_path": candidate["markdown_path"],
                "artifact_recorded": True,
                "event_recorded": True,
                "branch": payload.get("branch"),
                "base_branch": payload.get("base_branch"),
                "head_sha": payload.get("head_sha"),
            },
            "payload": payload,
            "reasons": [],
        },
    )


def _find_reusable_branch_push(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    store = TaskMirrorStore(request.db_path)
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(request.task_key)
        if artifact.artifact_type == BRANCH_PUSH_ARTIFACT_TYPE
    ]
    events = [
        event
        for event in store.list_task_events(request.task_key)
        if event.event_type == BRANCH_PUSH_EVENT_TYPE
    ]
    if not artifacts and not events:
        return _reuse_scan("none", _STAGE_BRANCH_PUSH)

    reasons: list[str] = []
    if not artifacts:
        reasons.append("branch_push_artifact_missing")
    if not events:
        reasons.append("branch_push_event_missing")

    candidates: list[dict[str, Any]] = []
    for artifact in artifacts:
        payload, artifact_reasons = _read_json_artifact(
            artifact.path,
            context="branch_push_artifact",
        )
        reasons.extend(artifact_reasons)
        if payload is None:
            continue
        candidate_reasons = _validate_branch_push_payload(
            payload,
            request=request,
            preflight=preflight,
            handoff=handoff,
        )
        if candidate_reasons:
            reasons.extend(
                f"branch_push_invalid:{artifact.path}: {reason}"
                for reason in candidate_reasons
            )
            continue
        candidates.append(
            {
                "artifact_path": str(artifact.path),
                "payload": payload,
            }
        )

    for event in events:
        payload, payload_reasons = _read_event_payload(
            event.payload_json,
            context="branch_push_event",
        )
        reasons.extend(payload_reasons)
        if payload is None:
            continue
        candidate_reasons = _validate_branch_push_payload(
            payload,
            request=request,
            preflight=preflight,
            handoff=handoff,
        )
        if candidate_reasons:
            reasons.extend(
                f"branch_push_event_invalid: {reason}"
                for reason in candidate_reasons
            )
            continue
        candidates.append(
            {
                "artifact_path": str(payload.get("artifact_path") or ""),
                "payload": payload,
            }
        )

    if reasons:
        return _reuse_scan("invalid", _STAGE_BRANCH_PUSH, reasons=reasons)

    unique_keys = {
        (
            candidate["payload"].get("remote"),
            candidate["payload"].get("branch"),
            candidate["payload"].get("base_branch"),
            candidate["payload"].get("head_sha")
            or candidate["payload"].get("pushed_commit_sha"),
        )
        for candidate in candidates
    }
    if len(unique_keys) != 1:
        return _reuse_scan(
            "ambiguous",
            _STAGE_BRANCH_PUSH,
            reasons=["branch_push_evidence_ambiguous"],
        )

    artifact_candidates = [
        candidate for candidate in candidates if candidate["artifact_path"]
    ]
    candidate = artifact_candidates[-1] if artifact_candidates else candidates[-1]
    payload = candidate["payload"]
    return _reuse_scan(
        "usable",
        _STAGE_BRANCH_PUSH,
        stage_result={
            "ok": True,
            "stage": _STAGE_BRANCH_PUSH,
            "summary": {
                "pushed": False,
                "reused": True,
                "already_pushed": True,
                "remote": payload.get("remote") or request.remote,
                "branch": payload.get("branch") or preflight["worktree"]["branch"],
                "artifact_path": candidate["artifact_path"] or None,
            },
            "payload": payload,
            "reasons": [],
        },
    )


def _find_reusable_draft_pr(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    branch_push: dict[str, Any],
) -> dict[str, Any]:
    existing = _find_existing_draft_pr_evidence(
        request,
        preflight=preflight,
        branch_push=branch_push,
    )
    if existing["state"] != "usable":
        return existing

    payload = existing["payload"]
    return _reuse_scan(
        "usable",
        _STAGE_DRAFT_PR,
        stage_result={
            "ok": True,
            "stage": _STAGE_DRAFT_PR,
            "summary": {
                "created": False,
                "reused": True,
                "already_created": True,
                "draft": True,
                "pr_url": payload.get("pr_url"),
                "pr_number": payload.get("pr_number"),
                "artifact_path": existing.get("artifact_path"),
            },
            "payload": payload,
            "reasons": [],
        },
    )


def _find_existing_draft_pr_evidence(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    branch_push: dict[str, Any],
) -> dict[str, Any]:
    store = TaskMirrorStore(request.db_path)
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(request.task_key)
        if artifact.artifact_type == DRAFT_PR_ARTIFACT_TYPE
    ]
    events = [
        event
        for event in store.list_task_events(request.task_key)
        if event.event_type == DRAFT_PR_EVENT_TYPE
    ]
    if not artifacts and not events:
        return _reuse_scan("none", _STAGE_DRAFT_PR)

    reasons: list[str] = []
    if not artifacts:
        reasons.append("draft_pr_artifact_missing")
    if not events:
        reasons.append("draft_pr_event_missing")

    candidates: list[dict[str, Any]] = []
    for artifact in artifacts:
        payload, artifact_reasons = _read_json_artifact(
            artifact.path,
            context="draft_pr_artifact",
        )
        reasons.extend(artifact_reasons)
        if payload is None:
            continue
        candidate_reasons = _validate_draft_pr_payload(
            payload,
            request=request,
            preflight=preflight,
            branch_push=branch_push,
        )
        if candidate_reasons:
            reasons.extend(
                f"draft_pr_invalid:{artifact.path}: {reason}"
                for reason in candidate_reasons
            )
            continue
        candidates.append({"artifact_path": str(artifact.path), "payload": payload})

    for event in events:
        payload, payload_reasons = _read_event_payload(
            event.payload_json,
            context="draft_pr_event",
        )
        reasons.extend(payload_reasons)
        if payload is None:
            continue
        candidate_reasons = _validate_draft_pr_payload(
            payload,
            request=request,
            preflight=preflight,
            branch_push=branch_push,
        )
        if candidate_reasons:
            reasons.extend(
                f"draft_pr_event_invalid: {reason}" for reason in candidate_reasons
            )
            continue
        candidates.append(
            {
                "artifact_path": str(payload.get("artifact_path") or ""),
                "payload": payload,
            }
        )

    if reasons:
        return _reuse_scan("invalid", _STAGE_DRAFT_PR, reasons=reasons)

    unique_keys = {
        (
            candidate["payload"].get("repo"),
            candidate["payload"].get("base_branch"),
            candidate["payload"].get("head_branch"),
            candidate["payload"].get("pr_number"),
            candidate["payload"].get("pr_url"),
        )
        for candidate in candidates
    }
    if len(unique_keys) != 1:
        return _reuse_scan(
            "ambiguous",
            _STAGE_DRAFT_PR,
            reasons=["draft_pr_evidence_ambiguous"],
        )

    artifact_candidates = [
        candidate for candidate in candidates if candidate["artifact_path"]
    ]
    candidate = artifact_candidates[-1] if artifact_candidates else candidates[-1]
    return _reuse_scan(
        "usable",
        _STAGE_DRAFT_PR,
        artifact_path=candidate["artifact_path"] or None,
        payload=candidate["payload"],
    )


def _validate_pr_handoff_payload(
    payload: dict[str, Any],
    *,
    request: PRPreparationPipelineRequest,
    preflight: dict[str, Any],
) -> list[str]:
    worktree = preflight["worktree"]
    expected_base = request.base_branch or worktree["base_branch"]
    expected_repo = preflight["repo"]
    reasons: list[str] = []
    if payload.get("artifact_type") != PR_HANDOFF_ARTIFACT_TYPE:
        reasons.append("artifact_type_mismatch")
    if payload.get("task_key") != request.task_key:
        reasons.append("task_key_mismatch")
    if payload.get("task_status") != "waiting_approval":
        reasons.append("task_status_not_waiting_approval")
    if payload.get("branch") != worktree["branch"]:
        reasons.append("branch_mismatch")
    if payload.get("base_branch") != expected_base:
        reasons.append("base_branch_mismatch")
    if expected_repo and payload.get("repo") != expected_repo:
        reasons.append("repo_mismatch")
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    for key in ("pr_created", "pushed", "merged", "cleanup_performed", "github_mutated"):
        if safety.get(key) is not False:
            reasons.append(f"safety.{key}_not_false")
    if safety.get("human_review_required") is not True:
        reasons.append("safety.human_review_required_not_true")
    return reasons


def _validate_branch_push_payload(
    payload: dict[str, Any],
    *,
    request: PRPreparationPipelineRequest,
    preflight: dict[str, Any],
    handoff: dict[str, Any],
) -> list[str]:
    worktree = preflight["worktree"]
    expected_base = request.base_branch or worktree["base_branch"]
    expected_head_sha = str(
        ((handoff.get("summary") or {}).get("head_sha") or "")
    ).strip()
    pushed_head_sha = str(
        payload.get("head_sha") or payload.get("pushed_commit_sha") or ""
    ).strip()
    reasons: list[str] = []
    if payload.get("artifact_type") != BRANCH_PUSH_ARTIFACT_TYPE:
        reasons.append("artifact_type_mismatch")
    if payload.get("task_key") != request.task_key:
        reasons.append("task_key_mismatch")
    if payload.get("remote") != request.remote:
        reasons.append("remote_mismatch")
    if payload.get("branch") != worktree["branch"]:
        reasons.append("branch_mismatch")
    if payload.get("base_branch") != expected_base:
        reasons.append("base_branch_mismatch")
    if expected_head_sha and pushed_head_sha != expected_head_sha:
        reasons.append("head_sha_mismatch")
    if payload.get("branch_pushed") is not True:
        reasons.append("branch_pushed_not_true")
    if payload.get("push_ok") is not True:
        reasons.append("push_ok_not_true")
    for key in ("pr_created", "merged", "approved", "cleanup_performed"):
        if payload.get(key) is not False:
            reasons.append(f"{key}_not_false")
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    if safety.get("branch_pushed") is not True:
        reasons.append("safety.branch_pushed_not_true")
    for key in ("pr_created", "merged", "approved", "cleanup_performed"):
        if safety.get(key) is not False:
            reasons.append(f"safety.{key}_not_false")
    for key in ("branch_deleted", "worktree_deleted", "force_push"):
        if key in safety and safety.get(key) is not False:
            reasons.append(f"safety.{key}_not_false")
    return reasons


def _validate_draft_pr_payload(
    payload: dict[str, Any],
    *,
    request: PRPreparationPipelineRequest,
    preflight: dict[str, Any],
    branch_push: dict[str, Any],
) -> list[str]:
    worktree = preflight["worktree"]
    expected_base = request.base_branch or worktree["base_branch"]
    expected_repo = preflight["repo"]
    reasons: list[str] = []
    if payload.get("artifact_type") != DRAFT_PR_ARTIFACT_TYPE:
        reasons.append("artifact_type_mismatch")
    if payload.get("task_key") != request.task_key:
        reasons.append("task_key_mismatch")
    if expected_repo and payload.get("repo") != expected_repo:
        reasons.append("repo_mismatch")
    if payload.get("base_branch") != expected_base:
        reasons.append("base_branch_mismatch")
    if payload.get("head_branch") != worktree["branch"]:
        reasons.append("head_branch_mismatch")
    if payload.get("draft") is not True:
        reasons.append("draft_not_true")
    if not payload.get("pr_url"):
        reasons.append("pr_url_missing")
    if not isinstance(payload.get("pr_number"), int):
        reasons.append("pr_number_missing")
    if payload.get("pr_created") is not True:
        reasons.append("pr_created_not_true")
    if payload.get("draft_pr_created") is not True:
        reasons.append("draft_pr_created_not_true")
    if payload.get("branch_push_verified") is not True:
        reasons.append("branch_push_verified_not_true")
    branch_push_artifact = (branch_push.get("summary") or {}).get("artifact_path")
    payload_branch_push_artifact = payload.get("branch_push_artifact_path")
    if (
        branch_push_artifact
        and payload_branch_push_artifact
        and payload_branch_push_artifact != branch_push_artifact
    ):
        reasons.append("branch_push_artifact_path_mismatch")
    for key in ("merged", "approved", "cleanup_performed"):
        if payload.get(key) is not False:
            reasons.append(f"{key}_not_false")
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    if safety.get("pr_created") is not True:
        reasons.append("safety.pr_created_not_true")
    if safety.get("draft_pr") is not True:
        reasons.append("safety.draft_pr_not_true")
    for key in ("merged", "approved", "cleanup_performed"):
        if safety.get(key) is not False:
            reasons.append(f"safety.{key}_not_false")
    for key in ("issue_closed", "branch_deleted", "worktree_deleted"):
        if key in safety and safety.get(key) is not False:
            reasons.append(f"safety.{key}_not_false")
    return reasons


def _read_event_payload(
    payload_json: str | None,
    *,
    context: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not payload_json:
        return None, [f"{context}_payload_missing"]
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None, [f"{context}_payload_json_malformed"]
    if not isinstance(payload, dict):
        return None, [f"{context}_payload_json_not_object"]
    return payload, []


def _reuse_scan(
    state: str,
    stage_name: str,
    *,
    reasons: list[str] | None = None,
    stage_result: dict[str, Any] | None = None,
    stage_payload: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    result = {
        "state": state,
        "stage_name": stage_name,
        "reasons": _unique_strings(reasons or []),
    }
    if stage_result is not None:
        result["stage"] = stage_result
    if stage_payload is not None:
        result["payload"] = stage_payload
    if payload is not None:
        result["payload"] = payload
    if artifact_path is not None:
        result["artifact_path"] = artifact_path
    return result


def _default_branch_push_fn(**kwargs: Any) -> dict[str, Any]:
    request = BranchPushConfirmRequest(
        task_key=str(kwargs["task_key"]),
        repo_path=Path(kwargs["repo_path"]),
        db_path=Path(kwargs["db_path"]),
        artifact_root=Path(kwargs["artifact_root"]),
        remote=str(kwargs.get("remote") or "origin"),
        branch=str(kwargs["branch"]) if kwargs.get("branch") else None,
        dry_run=bool(kwargs.get("dry_run", False)),
        confirm_branch_push=bool(kwargs.get("confirm_branch_push", False)),
    )
    return confirm_branch_push(request).to_dict()


def _default_draft_pr_fn(**kwargs: Any) -> dict[str, Any]:
    repo = str(kwargs.get("repo") or "").strip()
    if not repo:
        raise PRPreparationPipelineError("repo is required for draft PR creation")
    if kwargs.get("draft") is not True:
        raise PRPreparationPipelineError("only draft PR creation is supported")
    request = DraftPrConfirmRequest(
        task_key=str(kwargs["task_key"]),
        repo=repo,
        repo_path=Path(kwargs["repo_path"]),
        db_path=Path(kwargs["db_path"]),
        artifact_root=Path(kwargs["artifact_root"]),
        base=str(kwargs["base"]) if kwargs.get("base") else None,
        head=str(kwargs["head"]) if kwargs.get("head") else None,
        dry_run=bool(kwargs.get("dry_run", False)),
        confirm_draft_pr=bool(kwargs.get("confirm_draft_pr", False)),
    )
    return confirm_draft_pr(request).to_dict()


def _dry_run_response(
    request: PRPreparationPipelineRequest,
    *,
    preflight: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    branch = preflight["summary"].get("branch")
    return {
        "ok": True,
        "schema_version": PR_PREPARATION_PIPELINE_SCHEMA_VERSION,
        "source": PR_PREPARATION_PIPELINE_SOURCE,
        "status": "dry_run",
        "mode": "dry_run",
        "task_key": request.task_key,
        "would_prepare_pr": True,
        "stages": {
            _STAGE_PREFLIGHT: preflight["summary"],
            _STAGE_PR_HANDOFF: {
                "would_create": True,
                "artifact_path": handoff["summary"].get("artifact_path"),
            },
            _STAGE_BRANCH_PUSH: {
                "would_push": True,
                "remote": request.remote,
                "branch": branch,
            },
            _STAGE_DRAFT_PR: {
                "would_create": True,
                "draft": True,
                "base": request.base_branch or preflight["summary"].get("base_branch"),
                "head": branch,
            },
        },
        "safety": _safety(
            dry_run=True,
            resume_existing=request.resume_existing,
            github_mutated=False,
            branch_pushed=False,
            draft_pr_created=False,
        ),
    }


def _failure_response(
    request: PRPreparationPipelineRequest,
    *,
    failed_stage: str,
    reasons: list[str],
    stage_result: dict[str, Any] | None,
    stages: dict[str, Any] | None = None,
    branch_pushed: bool = False,
    draft_pr_created: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": PR_PREPARATION_PIPELINE_SCHEMA_VERSION,
        "source": PR_PREPARATION_PIPELINE_SOURCE,
        "status": "failed",
        "mode": "dry_run" if request.dry_run else "confirmed",
        "failed_stage": failed_stage,
        "task_key": request.task_key,
        "reasons": _unique_strings([str(reason) for reason in reasons if reason]),
        "stage_result": stage_result,
        "stages": stages or {},
        "safety": _safety(
            dry_run=request.dry_run,
            resume_existing=request.resume_existing,
            github_mutated=branch_pushed or draft_pr_created,
            branch_pushed=branch_pushed,
            draft_pr_created=draft_pr_created,
        ),
    }


def _stage_failure(
    stage: str,
    reasons: list[str],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "stage": stage,
        "summary": {
            "created": False,
            "pushed": False,
            "reasons": _unique_strings([str(reason) for reason in reasons if reason]),
        },
        "payload": payload,
        "reasons": _unique_strings([str(reason) for reason in reasons if reason]),
    }


def _safety(
    *,
    dry_run: bool,
    resume_existing: bool = False,
    github_mutated: bool,
    branch_pushed: bool,
    draft_pr_created: bool,
) -> dict[str, bool]:
    safety = dict(PR_PREPARATION_PIPELINE_SAFETY_FLAGS)
    safety["dry_run"] = dry_run
    safety["resume_existing"] = resume_existing
    safety["github_mutated"] = github_mutated
    safety["branch_pushed"] = branch_pushed
    safety["draft_pr_created"] = draft_pr_created
    safety["duplicate_draft_pr_created"] = False
    safety["approved"] = False
    safety["merged"] = False
    safety["cleanup_performed"] = False
    safety["human_review_required"] = True
    return safety


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
        if isinstance(payload, dict):
            return payload
    raise TypeError("injected helper must return dict-like result")


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "PR_PREPARATION_PIPELINE_SAFETY_FLAGS",
    "PR_PREPARATION_PIPELINE_SCHEMA_VERSION",
    "PR_PREPARATION_PIPELINE_SOURCE",
    "PRPreparationPipelineError",
    "PRPreparationPipelineRequest",
    "run_pr_preparation_pipeline",
]
