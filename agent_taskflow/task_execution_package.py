"""Deterministic Task Execution Package contract.

Given an already-queued TaskRecord, this module builds the minimum
executor-ready artifact package: a deterministic implementation prompt
markdown file and a task_execution_package.json descriptor. It records
both as TaskMirrorStore artifacts and emits a single
task_execution_package_created event.

This module does not:

- run any executor,
- prepare a workspace or worktree,
- run validators,
- push branches, create PRs, merge, approve, reject, or clean up,
- start a scheduler, polling loop, webhook handler, or background worker.

It bridges the gap between a Phase 6D ingested queued task and the
explicit approved_task_runner handoff. approved_task_runner.py blocks
opencode execution when implementation_prompt.md is missing; this
package generator deterministically creates that prompt and an audited
JSON descriptor next to it. The runner itself is not invoked here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.models import (
    TaskRecord,
    require_absolute_path,
    utc_now_iso,
)
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key


SCHEMA_VERSION = "task_execution_package.v1"
PACKAGE_FILENAME = "task_execution_package.json"
IMPLEMENTATION_PROMPT_FILENAME = "implementation_prompt.md"
ISSUE_SPEC_FILENAME = "issue_spec.md"

EVENT_TYPE = "task_execution_package_created"
EVENT_SOURCE = "task_execution_package"

PACKAGE_ARTIFACT_TYPE = "task_execution_package"
PROMPT_ARTIFACT_TYPE = "implementation_prompt"

GITHUB_INGESTION_EVENT_TYPE = "github_issue_ingested"
ISSUE_SPEC_ARTIFACT_TYPE = "issue_spec"

DEFAULT_REQUIRED_VALIDATORS: tuple[str, ...] = (
    "pytest",
    "policy",
    "changed-files",
)


class TaskExecutionPackageError(RuntimeError):
    """Raised when a task execution package cannot be built."""


@dataclass(frozen=True)
class TaskExecutionPackageRequest:
    """Request for deterministic task execution package creation."""

    task_key: str
    db_path: Path | None = None
    artifact_root: Path | None = None
    required_validators: tuple[str, ...] | None = None
    dry_run: bool = True
    confirm: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        if self.dry_run and self.confirm:
            raise ValueError(
                "dry_run and confirm are mutually exclusive"
            )

        if not self.dry_run and not self.confirm:
            raise ValueError(
                "confirmed package creation requires confirm=True"
            )

        if self.db_path is None:
            db_path = default_db_path()
        else:
            db_path = require_absolute_path(self.db_path, "db_path")
        object.__setattr__(self, "db_path", Path(db_path))

        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                require_absolute_path(self.artifact_root, "artifact_root"),
            )

        if self.required_validators is None:
            object.__setattr__(
                self,
                "required_validators",
                DEFAULT_REQUIRED_VALIDATORS,
            )
        else:
            normalized: list[str] = []
            for validator in self.required_validators:
                text = str(validator).strip()
                if not text:
                    raise ValueError(
                        "required_validators entries must be non-empty"
                    )
                normalized.append(text)
            object.__setattr__(
                self,
                "required_validators",
                tuple(normalized),
            )


def create_task_execution_package(
    request: TaskExecutionPackageRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> dict[str, Any]:
    """Build (and optionally persist) a task execution package."""

    current_store = store or TaskMirrorStore(request.db_path)

    task = current_store.get_task(request.task_key)
    if task is None:
        return _blocked_result(
            request,
            task=None,
            artifact_dir=None,
            reason=f"Task not found: {request.task_key}",
        )

    if task.status != "queued":
        return _blocked_result(
            request,
            task=task,
            artifact_dir=task.artifact_dir,
            reason=(
                "Task execution package requires status=queued; "
                f"current status: {task.status!r}"
            ),
        )

    try:
        artifact_dir = _resolve_artifact_dir(task, request)
    except TaskExecutionPackageError as exc:
        return _blocked_result(
            request,
            task=task,
            artifact_dir=task.artifact_dir,
            reason=str(exc),
        )

    source_evidence = _discover_source_evidence(current_store, task, artifact_dir)
    prompt_text = _render_implementation_prompt(
        task=task,
        artifact_dir=artifact_dir,
        source_evidence=source_evidence,
        required_validators=request.required_validators,
    )
    package_payload = _build_package_payload(
        task=task,
        artifact_dir=artifact_dir,
        source_evidence=source_evidence,
        required_validators=request.required_validators,
        dry_run=request.dry_run,
    )

    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
    package_path = artifact_dir / PACKAGE_FILENAME

    if request.dry_run:
        return _success_result(
            request,
            task=task,
            artifact_dir=artifact_dir,
            prompt_path=prompt_path,
            package_path=package_path,
            package_payload=package_payload,
            source_evidence=source_evidence,
            wrote=False,
        )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")
    package_path.write_text(
        json.dumps(package_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    _record_artifact_once(
        current_store,
        task.task_key,
        PROMPT_ARTIFACT_TYPE,
        prompt_path,
    )
    _record_artifact_once(
        current_store,
        task.task_key,
        PACKAGE_ARTIFACT_TYPE,
        package_path,
    )

    current_store.record_task_event(
        task.task_key,
        EVENT_TYPE,
        EVENT_SOURCE,
        message="Task execution package created",
        payload={
            "kind": EVENT_TYPE,
            "task_key": task.task_key,
            "artifact_dir": str(artifact_dir),
            "implementation_prompt_path": str(prompt_path),
            "package_path": str(package_path),
            "schema_version": SCHEMA_VERSION,
            "source_evidence": source_evidence,
            "required_validators": list(request.required_validators),
        },
    )

    return _success_result(
        request,
        task=task,
        artifact_dir=artifact_dir,
        prompt_path=prompt_path,
        package_path=package_path,
        package_payload=package_payload,
        source_evidence=source_evidence,
        wrote=True,
    )


def _resolve_artifact_dir(
    task: TaskRecord,
    request: TaskExecutionPackageRequest,
) -> Path:
    if task.artifact_dir is not None:
        return task.artifact_dir
    if request.artifact_root is not None:
        return request.artifact_root / task.task_key
    raise TaskExecutionPackageError(
        "Task has no artifact_dir and no artifact_root was supplied"
    )


def _discover_source_evidence(
    store: TaskMirrorStore,
    task: TaskRecord,
    artifact_dir: Path,
) -> dict[str, Any]:
    """Discover source context for the prompt without overfitting."""

    discovered: dict[str, Any] = {
        "issue_spec_artifact_path": None,
        "issue_spec_file_path": None,
        "github_issue_ingested_event": None,
        "title_fallback": None,
    }

    for record in store.list_task_artifacts(task.task_key):
        if record.artifact_type == ISSUE_SPEC_ARTIFACT_TYPE:
            discovered["issue_spec_artifact_path"] = str(record.path)
            break

    issue_spec_file = artifact_dir / ISSUE_SPEC_FILENAME
    if discovered["issue_spec_artifact_path"] is None and issue_spec_file.exists():
        discovered["issue_spec_file_path"] = str(issue_spec_file)

    if (
        discovered["issue_spec_artifact_path"] is None
        and discovered["issue_spec_file_path"] is None
    ):
        for event in store.list_task_events(task.task_key):
            if event.event_type != GITHUB_INGESTION_EVENT_TYPE:
                continue
            payload = _safe_json_object(event.payload_json)
            if not payload:
                continue
            discovered["github_issue_ingested_event"] = {
                "repo": payload.get("repo"),
                "issue_number": payload.get("issue_number"),
                "issue_url": payload.get("issue_url"),
                "title": payload.get("title"),
            }
            break

    if (
        discovered["issue_spec_artifact_path"] is None
        and discovered["issue_spec_file_path"] is None
        and discovered["github_issue_ingested_event"] is None
    ):
        discovered["title_fallback"] = task.title or task.task_key

    return discovered


def _render_implementation_prompt(
    *,
    task: TaskRecord,
    artifact_dir: Path,
    source_evidence: dict[str, Any],
    required_validators: tuple[str, ...],
) -> str:
    """Render the deterministic implementation prompt markdown."""

    title = task.title or task.task_key
    source_section = _render_source_section(source_evidence)
    validators_line = ", ".join(required_validators) if required_validators else "(none)"

    return "\n".join(
        [
            f"# Implementation Prompt — {task.task_key}",
            "",
            "This prompt is the deterministic input/spec evidence for the bounded",
            "executor. It is generated by agent_taskflow.task_execution_package.",
            "It is not implementation evidence, validation evidence, approval,",
            "PR creation, push, merge, or cleanup evidence.",
            "",
            "## Task",
            "",
            f"- Task key: {task.task_key}",
            f"- Title / goal: {title}",
            f"- Project: {task.project}",
            f"- Board: {task.board or '(none)'}",
            f"- Repository path: {task.repo_path}",
            f"- Artifact directory: {artifact_dir}",
            "",
            "## Required reading",
            "",
            "- Read AGENTS.md before editing.",
            "- Read WORKFLOW.md when the task involves task execution workflow,",
            "  executor behavior, validator behavior, proof-of-work artifacts,",
            "  workspace policy, changed-files or path policy, approval/blocking",
            "  behavior, Mission Control review semantics, or governance rules.",
            "",
            "## Source context",
            "",
            source_section,
            "",
            "## Scope boundaries",
            "",
            "- Prefer small, reviewable changes.",
            "- Reuse existing project patterns before introducing new abstractions.",
            "- Keep executor, validator, store, API, and frontend boundaries clean.",
            "- Do not edit unrelated files.",
            "- Do not perform cosmetic rewrites unrelated to the task.",
            "- Do not introduce new dependencies unless explicitly required.",
            "- Do not touch secrets, .env files, SSH keys, API keys, tokens, or",
            "  system credentials.",
            "- Do not weaken tests, validators, governance checks, or safety",
            "  policies.",
            "- Do not fake success, fake validation, or fabricate artifacts.",
            "",
            "## Validation expectations",
            "",
            f"- Required validators (default unless overridden): {validators_line}.",
            "- After code changes, run the most relevant validation.",
            "- For Python changes, prefer python3 -m unittest discover -s tests",
            "  and python3 -m compileall agent_taskflow scripts tests.",
            "- Never claim a command passed unless it was actually run and",
            "  observed to pass.",
            "",
            "## Governance constraints",
            "",
            "Do not do any of the following unless a human reviewer explicitly",
            "asks:",
            "",
            "- create commits",
            "- push",
            "- merge",
            "- create PRs",
            "- approve, reject, or mark work finally complete",
            "- delete branches or worktrees",
            "- run destructive cleanup",
            "- close issues",
            "- bypass validators",
            "- change deployment, systemd, nginx, or cron configuration",
            "",
            "Human review remains the final gate.",
            "",
            "## Final report expectations",
            "",
            "End the implementation task with a final report covering:",
            "",
            "1. Starting state (branch, git status)",
            "2. Implementation summary",
            "3. Files changed",
            "4. Validation commands run and their results",
            "5. Artifacts",
            "6. Final state (git status, commit created yes/no)",
            "7. Blockers / follow-ups",
            "",
        ]
    )


def _render_source_section(source_evidence: dict[str, Any]) -> str:
    lines: list[str] = []
    artifact_path = source_evidence.get("issue_spec_artifact_path")
    file_path = source_evidence.get("issue_spec_file_path")
    event = source_evidence.get("github_issue_ingested_event")
    title_fallback = source_evidence.get("title_fallback")

    if artifact_path:
        lines.append(f"- Recorded issue/spec artifact: {artifact_path}")
        lines.append("- Read this artifact for the full source intent.")
    elif file_path:
        lines.append(f"- Local issue/spec file: {file_path}")
        lines.append("- Read this file for the full source intent.")
    elif event:
        repo = event.get("repo") or "(unknown repo)"
        issue_number = event.get("issue_number")
        issue_url = event.get("issue_url") or "(none)"
        title = event.get("title") or "(no title)"
        lines.append(
            f"- GitHub issue: {repo}#{issue_number} — {title}"
        )
        lines.append(f"- Issue URL: {issue_url}")
    elif title_fallback:
        lines.append(f"- No recorded issue/spec artifact or ingestion event.")
        lines.append(f"- Treat the task title as the source intent: {title_fallback}")
    else:
        lines.append("- No source context available beyond this prompt.")
    return "\n".join(lines)


def _build_package_payload(
    *,
    task: TaskRecord,
    artifact_dir: Path,
    source_evidence: dict[str, Any],
    required_validators: tuple[str, ...],
    dry_run: bool,
) -> dict[str, Any]:
    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
    package_path = artifact_dir / PACKAGE_FILENAME
    return {
        "schema_version": SCHEMA_VERSION,
        "task_key": task.task_key,
        "project": task.project,
        "board": task.board,
        "title": task.title,
        "status_before": task.status,
        "repo_path": str(task.repo_path),
        "artifact_dir": str(artifact_dir),
        "implementation_prompt_path": str(prompt_path),
        "package_path": str(package_path),
        "source_evidence": source_evidence,
        "required_validators": list(required_validators),
        "executor_hint": task.executor,
        "model": task.model,
        "provider": task.provider,
        "tools": list(task.tools) if task.tools else None,
        "pi_bin": task.pi_bin,
        "created_at": utc_now_iso(),
        "dry_run": dry_run,
        "safety": {
            "execution_package_only": True,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "background_worker_started": False,
        },
    }


def _record_artifact_once(
    store: TaskMirrorStore,
    task_key: str,
    artifact_type: str,
    path: Path,
) -> None:
    existing = {
        (record.artifact_type, str(record.path))
        for record in store.list_task_artifacts(task_key)
    }
    if (artifact_type, str(path)) in existing:
        return
    store.record_task_artifact(task_key, artifact_type, path)


def _safe_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safety_block(
    *,
    dry_run: bool,
    wrote: bool,
) -> dict[str, Any]:
    return {
        "read_only": dry_run,
        "db_written": wrote,
        "artifact_written": wrote,
        "execution_package_created": wrote,
        "implementation_prompt_created": wrote,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _blocked_result(
    request: TaskExecutionPackageRequest,
    *,
    task: TaskRecord | None,
    artifact_dir: Path | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "mode": "dry_run" if request.dry_run else "confirmed",
        "task_key": request.task_key,
        "task_status_before": task.status if task is not None else None,
        "artifact_dir": str(artifact_dir) if artifact_dir is not None else None,
        "implementation_prompt_path": None,
        "package_path": None,
        "package": None,
        "source_evidence": None,
        "error": reason,
        "safety": _safety_block(dry_run=request.dry_run, wrote=False),
    }


def _success_result(
    request: TaskExecutionPackageRequest,
    *,
    task: TaskRecord,
    artifact_dir: Path,
    prompt_path: Path,
    package_path: Path,
    package_payload: dict[str, Any],
    source_evidence: dict[str, Any],
    wrote: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "mode": "dry_run" if request.dry_run else "confirmed",
        "task_key": task.task_key,
        "task_status_before": task.status,
        "artifact_dir": str(artifact_dir),
        "implementation_prompt_path": str(prompt_path),
        "package_path": str(package_path),
        "package": package_payload,
        "source_evidence": source_evidence,
        "error": None,
        "safety": _safety_block(dry_run=request.dry_run, wrote=wrote),
    }


__all__ = [
    "DEFAULT_REQUIRED_VALIDATORS",
    "EVENT_SOURCE",
    "EVENT_TYPE",
    "IMPLEMENTATION_PROMPT_FILENAME",
    "PACKAGE_ARTIFACT_TYPE",
    "PACKAGE_FILENAME",
    "PROMPT_ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "TaskExecutionPackageError",
    "TaskExecutionPackageRequest",
    "create_task_execution_package",
]
