"""Deterministic local PR handoff package generation.

This module assembles local handoff evidence for tasks that have already
reached the human review gate. It does not push, create pull requests, merge,
prepare workspaces, dispatch tasks, clean up worktrees, or mutate GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any

from agent_taskflow.api.review import build_review_evidence
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


SCHEMA_VERSION = "1"
ARTIFACT_TYPE = "pr_handoff"
EVENT_TYPE = "pr_handoff_created"
SOURCE = "pr_handoff"


class PrHandoffError(RuntimeError):
    """Raised when a PR handoff package cannot be safely generated."""


@dataclass(frozen=True)
class PrHandoffRequest:
    """Request for creating a local PR handoff package."""

    task_key: str
    db_path: Path | None = None
    output_dir: Path | None = None
    repo: str | None = None
    base_branch: str | None = None
    dry_run: bool = False
    require_waiting_approval: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                ensure_absolute_path(self.db_path, name="db_path"),
            )
        if self.output_dir is not None:
            object.__setattr__(
                self,
                "output_dir",
                ensure_absolute_path(self.output_dir, name="output_dir"),
            )


@dataclass(frozen=True)
class PrHandoffPackage:
    """In-memory representation of the local handoff package."""

    data: dict[str, Any]
    markdown: str


@dataclass(frozen=True)
class PrHandoffResult:
    """Result of a PR handoff generation attempt."""

    ok: bool
    task_key: str
    status: str
    summary: str
    package: PrHandoffPackage | None
    output_dir: Path
    json_path: Path
    markdown_path: Path
    dry_run: bool
    artifact_recorded: bool
    event_recorded: bool

    def to_summary_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "task_key": self.task_key,
            "status": self.status,
            "summary": self.summary,
            "output_dir": str(self.output_dir),
            "json_path": str(self.json_path),
            "markdown_path": str(self.markdown_path),
            "dry_run": self.dry_run,
            "artifact_recorded": self.artifact_recorded,
            "event_recorded": self.event_recorded,
        }
        if self.package is not None:
            payload["package"] = {
                "schema_version": self.package.data.get("schema_version"),
                "artifact_type": self.package.data.get("artifact_type"),
                "task_key": self.package.data.get("task_key"),
                "task_status": self.package.data.get("task_status"),
                "branch": self.package.data.get("branch"),
                "base_branch": self.package.data.get("base_branch"),
                "base_sha": self.package.data.get("base_sha"),
                "head_sha": self.package.data.get("head_sha"),
                "changed_files": self.package.data.get("changed_files", []),
                "proposed_pr": self.package.data.get("proposed_pr"),
                "safety": self.package.data.get("safety"),
            }
        return payload


def create_pr_handoff(
    request: PrHandoffRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> PrHandoffResult:
    """Create a deterministic local PR handoff package."""

    current_store = store or TaskMirrorStore(request.db_path)
    current_store.init_db()

    task = current_store.get_task(request.task_key)
    if task is None:
        raise PrHandoffError(f"Task not found: {request.task_key}")
    if request.require_waiting_approval and task.status != "waiting_approval":
        raise PrHandoffError(
            f"Task {task.task_key} must be waiting_approval, got {task.status}"
        )

    worktree = current_store.get_task_worktree(task.task_key)
    if worktree is None:
        raise PrHandoffError(f"TaskWorktreeRecord missing for task: {task.task_key}")
    if not worktree.worktree_path.is_dir():
        raise PrHandoffError(f"Worktree path is missing: {worktree.worktree_path}")
    if not worktree.branch.strip():
        raise PrHandoffError("TaskWorktreeRecord branch is required")

    base_branch = request.base_branch or worktree.base_branch or "main"
    if not base_branch.strip():
        raise PrHandoffError("base_branch is required")
    if not worktree.base_sha:
        raise PrHandoffError("TaskWorktreeRecord base_sha is required")

    head_sha = _git(worktree.worktree_path, ["rev-parse", "HEAD"])
    status_short = _git(worktree.worktree_path, ["status", "--short"])
    diff_names = _git(worktree.worktree_path, ["diff", "--name-only"])
    diff_stat = _git(worktree.worktree_path, ["diff", "--stat"])
    changed_files = _changed_files(status_short, diff_names)

    review_evidence = _review_evidence(current_store, task)
    validation_results = current_store.list_validation_results(task.task_key)
    executor_runs = current_store.list_executor_runs(task.task_key)
    artifacts = current_store.list_task_artifacts(task.task_key)

    output_root = request.output_dir or _default_output_root(task.artifact_dir)
    package_dir = output_root / task.task_key
    json_path = package_dir / "pr_handoff.json"
    markdown_path = package_dir / "pr_handoff.md"
    generated_at = utc_now_iso()

    package_data = _build_package_data(
        task=task,
        worktree=worktree,
        repo=request.repo,
        base_branch=base_branch,
        base_sha=worktree.base_sha,
        head_sha=head_sha,
        status_short=status_short,
        diff_stat=diff_stat,
        changed_files=changed_files,
        validation_results=validation_results,
        executor_runs=executor_runs,
        artifacts=artifacts,
        review_evidence=review_evidence,
        generated_at=generated_at,
    )
    markdown = _build_markdown(package_data)
    package = PrHandoffPackage(data=package_data, markdown=markdown)

    artifact_recorded = False
    event_recorded = False
    if not request.dry_run:
        package_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(package_data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(markdown, encoding="utf-8")
        artifact_recorded = _record_artifact_once(current_store, task.task_key, json_path)
        event_recorded = _record_event_once(
            current_store,
            task.task_key,
            json_path=json_path,
            markdown_path=markdown_path,
        )

    status = "dry_run" if request.dry_run else "created"
    return PrHandoffResult(
        ok=True,
        task_key=task.task_key,
        status=status,
        summary=f"PR handoff package {status} for {task.task_key}",
        package=package,
        output_dir=package_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        dry_run=request.dry_run,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
    )


def _default_output_root(artifact_dir: Path | None) -> Path:
    if artifact_dir is None:
        raise PrHandoffError("Task artifact_dir is required when --output-dir is omitted")
    return artifact_dir.parent / "pr_handoff"


def _git(worktree_path: Path, args: list[str]) -> str:
    allowed = {
        ("rev-parse", "HEAD"),
        ("status", "--short"),
        ("diff", "--name-only"),
        ("diff", "--stat"),
    }
    if tuple(args) not in allowed:
        raise PrHandoffError(f"Git command is not allowed: git {' '.join(args)}")

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
        raise PrHandoffError(
            f"git {' '.join(args)} failed with {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _changed_files(status_short: str, diff_names: str) -> list[str]:
    names: set[str] = set()
    for raw_line in status_short.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        name = line[3:] if len(line) > 3 else line
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name:
            names.add(name)
    for raw_line in diff_names.splitlines():
        name = raw_line.strip()
        if name:
            names.add(name)
    return sorted(names)


def _review_evidence(store: TaskMirrorStore, task: Any) -> dict[str, Any]:
    if task.artifact_dir is None:
        raise PrHandoffError("Task has no artifact directory; review evidence is unavailable")
    if not task.artifact_dir.is_dir():
        raise PrHandoffError(f"Task artifact directory is missing: {task.artifact_dir}")

    evidence = build_review_evidence(
        task_key=task.task_key,
        artifact_dir=task.artifact_dir,
        validation_results=store.list_validation_results(task.task_key),
    )
    contract = evidence.get("mission_contract", {})
    if contract.get("status") != "present":
        raise PrHandoffError("Review evidence is missing a present mission contract")
    if not evidence.get("validator_results"):
        raise PrHandoffError("Review evidence is missing validator results")
    if not evidence.get("artifacts"):
        raise PrHandoffError("Review evidence is missing artifact summaries")
    return evidence


def _build_package_data(
    *,
    task: Any,
    worktree: Any,
    repo: str | None,
    base_branch: str,
    base_sha: str,
    head_sha: str,
    status_short: str,
    diff_stat: str,
    changed_files: list[str],
    validation_results: list[dict[str, Any]],
    executor_runs: list[dict[str, Any]],
    artifacts: list[Any],
    review_evidence: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    title = f"{task.task_key}: {task.title or 'Task handoff'}"
    body = _proposed_pr_body(task, review_evidence)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": task.task_key,
        "task_title": task.title,
        "task_status": task.status,
        "project": task.project,
        "repo": repo,
        "repo_path": str(task.repo_path),
        "worktree_path": str(worktree.worktree_path),
        "branch": worktree.branch,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "git_status_short": status_short.splitlines(),
        "git_diff_stat": diff_stat,
        "changed_files": changed_files,
        "validation_summary": _validation_summary(validation_results),
        "executor_summary": _executor_summary(executor_runs),
        "artifact_summary": _artifact_summary(artifacts, review_evidence),
        "review_evidence_summary": _review_evidence_summary(review_evidence),
        "proposed_pr": {
            "title": title,
            "body": body,
            "base_branch": base_branch,
            "head_branch": worktree.branch,
            "draft_recommended": True,
            "create_command_preview": _create_command_preview(
                title=title,
                body=body,
                base_branch=base_branch,
                head_branch=worktree.branch,
            ),
        },
        "safety": {
            "pr_created": False,
            "pushed": False,
            "merged": False,
            "cleanup_performed": False,
            "github_mutated": False,
            "human_review_required": True,
        },
        "generated_at": generated_at,
    }


def _validation_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(results),
        "statuses": [
            {
                "validator": item.get("validator"),
                "status": item.get("status"),
                "exit_code": item.get("exit_code"),
                "summary": item.get("summary"),
                "log_path": item.get("log_path"),
                "created_at": item.get("created_at"),
            }
            for item in results
        ],
        "all_passed": bool(results)
        and all(item.get("status") == "passed" for item in results),
    }


def _executor_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    latest = runs[-1] if runs else None
    return {
        "count": len(runs),
        "latest": latest,
        "runs": runs,
    }


def _artifact_summary(artifacts: list[Any], review_evidence: dict[str, Any]) -> dict[str, Any]:
    db_artifacts = [
        {
            "artifact_type": artifact.artifact_type,
            "path": str(artifact.path),
            "created_at": artifact.created_at,
        }
        for artifact in artifacts
    ]
    evidence_artifacts = review_evidence.get("artifacts", [])
    return {
        "db_artifact_count": len(db_artifacts),
        "db_artifacts": db_artifacts,
        "review_artifact_count": len(evidence_artifacts),
        "review_artifacts": evidence_artifacts,
    }


def _review_evidence_summary(review_evidence: dict[str, Any]) -> dict[str, Any]:
    contract = review_evidence.get("mission_contract", {})
    validators = review_evidence.get("validator_results", [])
    artifacts = review_evidence.get("artifacts", [])
    policy = review_evidence.get("workflow_policy_evidence", {})
    return {
        "available": True,
        "mission_contract_status": contract.get("status"),
        "mission_contract_executor": contract.get("executor"),
        "human_approval_required": contract.get("human_approval_required"),
        "validator_result_count": len(validators),
        "artifact_count": len(artifacts),
        "policy_status": review_evidence.get("policy_status"),
        "policy_warnings": review_evidence.get("policy_warnings", []),
        "workflow_policy_evidence_available": policy.get("available", False),
    }


def _proposed_pr_body(task: Any, review_evidence: dict[str, Any]) -> str:
    validators = review_evidence.get("validator_results", [])
    validator_lines = [
        f"- {item.get('validator')}: {item.get('status')}"
        for item in validators
    ]
    if not validator_lines:
        validator_lines = ["- No validator results recorded."]
    return "\n".join(
        [
            f"Task: {task.task_key}",
            f"Title: {task.title or ''}",
            "",
            "Validation:",
            *validator_lines,
            "",
            "This PR should remain draft until a human/operator reviews the local handoff evidence.",
        ]
    )


def _create_command_preview(
    *,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
) -> str:
    parts = [
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
    return " ".join(shlex.quote(part) for part in parts)


def _build_markdown(data: dict[str, Any]) -> str:
    validations = data["validation_summary"]["statuses"]
    executors = data["executor_summary"]["runs"]
    artifacts = data["artifact_summary"]["db_artifacts"]
    changed_files = data["changed_files"] or ["(none)"]

    lines = [
        "# PR Handoff",
        "",
        "## Task Summary",
        "",
        f"- Task: {data['task_key']}",
        f"- Title: {data.get('task_title') or ''}",
        f"- Status: {data['task_status']}",
        f"- Project: {data['project']}",
        "",
        "## Branch / Worktree / Base",
        "",
        f"- Repo path: {data['repo_path']}",
        f"- Worktree path: {data['worktree_path']}",
        f"- Branch: {data['branch']}",
        f"- Base branch: {data['base_branch']}",
        f"- Base SHA: {data['base_sha']}",
        f"- Head SHA: {data['head_sha']}",
        "",
        "## Validation Status",
        "",
    ]
    lines.extend(
        f"- {item.get('validator')}: {item.get('status')} ({item.get('summary') or ''})"
        for item in validations
    )
    lines.extend(["", "## Executor Run Summary", ""])
    lines.extend(
        f"- {item.get('executor')}: {item.get('status')} ({item.get('summary') or ''})"
        for item in executors
    )
    lines.extend(["", "## Artifact List", ""])
    lines.extend(
        f"- {item.get('artifact_type')}: {item.get('path')}"
        for item in artifacts
    )
    lines.extend(["", "## Changed Files", ""])
    lines.extend(f"- {name}" for name in changed_files)
    lines.extend(
        [
            "",
            "## Proposed PR",
            "",
            f"- Title: {data['proposed_pr']['title']}",
            f"- Base: {data['proposed_pr']['base_branch']}",
            f"- Head: {data['proposed_pr']['head_branch']}",
            f"- Draft recommended: {data['proposed_pr']['draft_recommended']}",
            "",
            "```text",
            data["proposed_pr"]["body"],
            "```",
            "",
            "## Manual Next Steps",
            "",
            "- Inspect this handoff package and the referenced proof-of-work artifacts.",
            "- Inspect the local worktree diff before any GitHub action.",
            "- Create a draft PR only after human/operator review.",
            "- Keep merge and cleanup under explicit human or deterministic policy control.",
            "",
            "## Safety Warning",
            "",
            "- This package did not create a PR.",
            "- This package did not push.",
            "- This package did not merge.",
            "- Human/operator must inspect before any GitHub action.",
            "",
        ]
    )
    return "\n".join(lines)


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
) -> bool:
    for event in store.list_task_events(task_key):
        if event.event_type != EVENT_TYPE:
            continue
        try:
            payload = json.loads(event.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("json_path") == str(json_path):
            return False

    store.record_task_event(
        task_key,
        EVENT_TYPE,
        SOURCE,
        message="PR handoff package created",
        payload={
            "kind": EVENT_TYPE,
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "safety": {
                "pr_created": False,
                "pushed": False,
                "merged": False,
                "cleanup_performed": False,
                "github_mutated": False,
                "human_review_required": True,
            },
        },
    )
    return True
