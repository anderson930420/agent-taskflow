"""Pure, importable helpers for ``agent_taskflow.remote_branch_cleanup_confirm``.

This module holds deterministic, side-effect-free pieces of the remote branch
cleanup confirmation flow: git command construction, branch name
normalization/validation, protected branch constants, evidence dict assembly,
and small list/string utilities.

It intentionally does not touch subprocess, the SQLite store, the filesystem,
or any executor/validator/intake/scheduler/worktree state. The orchestration
logic in ``remote_branch_cleanup_confirm`` composes these helpers with the
I/O-bearing pieces.
"""

from __future__ import annotations

import json
import re
from typing import Any


PROTECTED_BRANCHES: set[str] = {"main", "master", "trunk"}

LOCAL_ARTIFACT_KIND = "local_cleanup"
LOCAL_EVENT_TYPE = "local_cleanup_completed"
LOCAL_CONFIRM_FLAG = "--confirm-local-cleanup"


def normalize_branch_name(value: Any) -> str | None:
    """Return stripped branch name, or None if value is not a non-empty string."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def validate_branch_name(branch: str) -> str | None:
    """Return an error string if branch is not a safe task branch name, else None."""
    if not branch:
        return "Branch name is missing"
    if branch.startswith("-"):
        return "Branch name must not start with '-'"
    if any(ch.isspace() for ch in branch):
        return "Branch name must not contain whitespace"
    if ".." in branch:
        return "Branch name must not contain '..'"
    if ":" in branch:
        return "Branch name must not contain ':'"
    if "*" in branch:
        return "Branch name must not contain '*'"
    if any(ch in branch for ch in {"?", "[", "]", "\\", "^", "~"}):
        return "Branch name contains unsupported git ref characters"
    if branch.endswith(".lock"):
        return "Branch name must not end with .lock"
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?", branch):
        return "Branch name is not a safe task branch name"
    return None


def dedupe_preserve_order(values: list[str]) -> list[str]:
    """Return values with duplicates and empty strings removed, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def build_git_ls_remote_heads_command(remote: str, branch: str) -> list[str]:
    """Build a git ls-remote --heads command for checking remote branch existence."""
    return ["git", "ls-remote", "--heads", remote, branch]


def build_git_push_delete_command(remote: str, branch: str) -> list[str]:
    """Build a git push --delete command for removing a remote branch."""
    return ["git", "push", remote, "--delete", branch]


def safety_block(
    *,
    human_confirmation_confirmed: bool,
    remote_branch_cleanup_performed: bool,
    remote_branch_deleted: bool,
) -> dict[str, Any]:
    """Build the standard safety block dict for a remote branch cleanup result."""
    return {
        "human_confirmation_required": True,
        "human_confirmation_confirmed": human_confirmation_confirmed,
        "task_status_changed": False,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "local_cleanup_performed": False,
        "worktree_removed": False,
        "local_branch_deleted": False,
        "remote_branch_cleanup_performed": remote_branch_cleanup_performed,
        "remote_branch_deleted": remote_branch_deleted,
        "github_issue_mutated": False,
        "issue_closed": False,
        "task_archived": False,
        "task_completed": False,
        "merged": False,
        "approved": False,
        "force_delete": False,
        "background_worker_started": False,
        "webhook_started": False,
        "polling_loop_started": False,
    }


def empty_cleanup_recommendation() -> dict[str, Any]:
    """Return an empty cleanup recommendation dict (unavailable state)."""
    return {
        "available": False,
        "status": None,
        "merged": False,
        "remote_branch_cleanup_recommended": False,
        "recommended_cleanup": [],
        "blocking_warnings": [],
        "non_blocking_warnings": [],
        "next_allowed_actions": [],
        "actions_not_performed": [],
        "summary": {},
        "safety": {},
    }


def empty_draft_pr_evidence() -> dict[str, Any]:
    """Return an empty draft PR evidence dict (unavailable state)."""
    return {
        "available": False,
        "artifact_recorded": False,
        "event_recorded": False,
        "artifact_path": None,
        "repo": None,
        "pr_number": None,
        "pr_url": None,
        "base_branch": None,
        "head_branch": None,
        "merged": None,
        "cleanup_performed": None,
        "issue_closed": None,
        "requires_human_confirmation": None,
        "warnings": ["Draft PR evidence is missing"],
    }


def empty_local_cleanup_evidence() -> dict[str, Any]:
    """Return an empty local cleanup evidence dict (unavailable state)."""
    return {
        "available": False,
        "artifact_recorded": False,
        "event_recorded": False,
        "artifact_path": None,
        "event_type": LOCAL_EVENT_TYPE,
        "artifact_kind": LOCAL_ARTIFACT_KIND,
        "payload": {},
        "local_branch": None,
        "cleanup_scope": None,
        "worktree_removed": None,
        "local_branch_deleted": None,
        "remote_branch_deleted": None,
        "issue_closed": None,
        "task_status_changed": None,
        "task_completed": None,
        "task_archived": None,
        "requires_human_confirmation": None,
        "confirmation_flag": LOCAL_CONFIRM_FLAG,
        "task_status": None,
        "warnings": ["Local cleanup evidence is missing"],
    }


def empty_remote_branch(remote: str, branch: str | None = None) -> dict[str, Any]:
    """Return an empty remote branch dict (unavailable/unknown state)."""
    return {
        "available": False,
        "remote": remote,
        "name": branch,
        "base_branch": None,
        "exists_before": False,
        "exists_after": False,
        "safe_to_delete": False,
        "deleted": False,
        "delete_attempted": False,
        "delete_error": None,
        "protected": False,
        "is_empty": False,
        "warnings": [],
    }


def cleanup_recommendation_snapshot(result: Any) -> dict[str, Any]:
    """Build a snapshot dict from a PostMergeCleanupRecommendationResult."""
    remote_cleanup_item = next(
        (
            item
            for item in result.recommended_cleanup
            if isinstance(item, dict) and item.get("action") == "delete_remote_branch"
        ),
        None,
    )
    return {
        "available": bool(getattr(result, "ok", False)),
        "status": result.status,
        "merged": bool(result.summary.get("merged")),
        "remote_branch_cleanup_recommended": bool(
            remote_cleanup_item and remote_cleanup_item.get("recommended")
        ),
        "recommended_cleanup": result.recommended_cleanup,
        "blocking_warnings": list(result.blocking_warnings),
        "non_blocking_warnings": list(result.non_blocking_warnings),
        "next_allowed_actions": list(result.next_allowed_actions),
        "actions_not_performed": list(result.actions_not_performed),
        "summary": result.summary,
        "safety": result.safety,
    }


def latest_event_payload(events: list[Any]) -> dict[str, Any]:
    """Parse the payload_json of the last event; return {} on missing/malformed JSON."""
    if not events:
        return {}
    payload_json = events[-1].payload_json
    if not payload_json:
        return {}
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload
