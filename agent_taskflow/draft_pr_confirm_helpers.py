"""Pure, importable helpers for ``agent_taskflow.draft_pr_confirm``.

This module holds the deterministic, side-effect-free pieces of the draft PR
confirmation flow: gh command construction, JSON parsing, PR-verification
dict assembly, repo/branch normalization, and small text helpers.

It intentionally does not touch subprocess, the SQLite store, the filesystem,
or any executor/validator/intake/scheduler/worktree state. The CLI entrypoint
in ``draft_pr_confirm`` composes these helpers with the I/O-bearing pieces.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from agent_taskflow._helpers import dedupe_preserve_order


PROTECTED_HEAD_BRANCHES = {"main", "master"}


class DraftPrConfirmError(RuntimeError):
    """Raised when a draft PR cannot be safely created."""


def build_gh_create_command(
    *, repo: str, base: str, head: str, title: str, body: str
) -> list[str]:
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


def build_gh_view_command(repo: str, pr_ref: str) -> list[str]:
    return [
        "gh",
        "pr",
        "view",
        pr_ref,
        "--repo",
        repo,
        "--json",
        "url,number,headRefName,headRefOid,baseRefName,isDraft,title,body,state,commits,files",
    ]


def build_gh_list_open_pr_command(*, repo: str, head: str) -> list[str]:
    return [
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


def build_gh_compare_command(*, repo: str, base: str, head: str) -> list[str]:
    """Build a ``gh api repos/{repo}/compare/{base}...{head}`` command.

    Used to fetch the ahead-commits / ahead-files diff between the target
    repo's base branch and the task head branch for PR verification. The
    compare endpoint is authoritative when ``gh pr view`` returns stale
    commits/files (for example after base-branch fast-forwards on origin).
    """

    if not repo.strip():
        raise DraftPrConfirmError("repo must not be empty for compare")
    if not base.strip():
        raise DraftPrConfirmError("base must not be empty for compare")
    if not head.strip():
        raise DraftPrConfirmError("head must not be empty for compare")
    return [
        "gh",
        "api",
        f"repos/{repo}/compare/{base}...{head}",
    ]


def command_preview(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def extract_pr_url(stdout: str) -> str:
    for match in re.findall(r"https://github\.com/[^\s]+/pull/\d+", stdout):
        return match.rstrip()
    raise DraftPrConfirmError("gh pr create did not print a created PR URL")


def extract_pr_file_paths(files_value: Any) -> list[str]:
    if not isinstance(files_value, list):
        return []
    paths: list[str] = []
    for item in files_value:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
    return paths


def extract_pr_commit_oids(commits_value: Any) -> list[str]:
    if not isinstance(commits_value, list):
        return []
    oids: list[str] = []
    for item in commits_value:
        if not isinstance(item, dict):
            continue
        oid = item.get("oid")
        if isinstance(oid, str) and oid.strip():
            oids.append(oid.strip())
    return oids


def extract_compare_file_paths(files_value: Any) -> list[str]:
    """Extract changed-file paths from a ``gh api .../compare/...`` response."""

    if not isinstance(files_value, list):
        return []
    paths: list[str] = []
    for item in files_value:
        if not isinstance(item, dict):
            continue
        path = item.get("filename")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
    return paths


def extract_compare_commit_shas(commits_value: Any) -> list[str]:
    """Extract commit SHAs from a ``gh api .../compare/...`` response."""

    if not isinstance(commits_value, list):
        return []
    shas: list[str] = []
    for item in commits_value:
        if not isinstance(item, dict):
            continue
        sha = item.get("sha")
        if isinstance(sha, str) and sha.strip():
            shas.append(sha.strip())
    return shas


def stringify_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
    return result


def parse_json_object(stdout: str, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DraftPrConfirmError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DraftPrConfirmError(f"{source} returned non-object JSON")
    return payload


def parse_json_array(stdout: str, *, source: str) -> list[Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DraftPrConfirmError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise DraftPrConfirmError(f"{source} returned non-array JSON")
    return payload


def parse_event_payload(payload_json: str | None, *, event_type: str) -> dict[str, Any]:
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


def body_preview(text: str, *, limit: int = 240) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:limit]


def normalize_repo(repo: str) -> str:
    normalized = repo.strip()
    if not normalized:
        raise ValueError("repo must not be empty")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise ValueError("repo must be a simple owner/name string")
    if normalized.count("/") != 1:
        raise ValueError("repo must be an owner/name string")
    return normalized


def normalize_branch_choice(
    *, provided: str | None, canonical: str, field_name: str
) -> str:
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


def empty_verification_preview() -> dict[str, Any]:
    return {
        "required": True,
        "post_create_verification_required": True,
        "expected_repo": None,
        "expected_base": None,
        "expected_head": None,
        "expected_title": None,
        "expected_files": [],
        "expected_commits": [],
        "expected_state": "OPEN",
        "expected_is_draft": True,
    }


def empty_verification_result(*, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    preview = expected or empty_verification_preview()
    return {
        "performed": False,
        "passed": False,
        "verified": False,
        "expected_base": preview.get("expected_base"),
        "actual_base": None,
        "expected_head": preview.get("expected_head"),
        "actual_head": None,
        "expected_title": preview.get("expected_title"),
        "actual_title": None,
        "expected_state": preview.get("expected_state"),
        "actual_state": None,
        "expected_is_draft": preview.get("expected_is_draft"),
        "actual_is_draft": None,
        "expected_files": list(preview.get("expected_files", [])),
        "actual_files": [],
        "missing_files": list(preview.get("expected_files", [])),
        "unexpected_files": [],
        "expected_commits": list(preview.get("expected_commits", [])),
        "actual_commits": [],
        "missing_commits": list(preview.get("expected_commits", [])),
        "unexpected_commits": [],
        "files_match": False,
        "commits_match": False,
        "base_match": False,
        "head_match": False,
        "title_match": False,
        "draft_match": False,
        "state_match": False,
        "blocking_warnings": [],
    }


def build_verification_result(
    payload: dict[str, Any],
    *,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Compare a ``gh pr view`` JSON payload against expected PR metadata."""

    actual_files = extract_pr_file_paths(payload.get("files"))
    actual_commits = extract_pr_commit_oids(payload.get("commits"))
    expected_files = sorted(stringify_list(expected.get("expected_files")))
    actual_files_sorted = sorted(actual_files)
    expected_commits = stringify_list(expected.get("expected_commits"))
    expected_commits_set = set(expected_commits)
    actual_commits_set = set(actual_commits)
    missing_files = [path for path in expected_files if path not in actual_files_sorted]
    unexpected_files = [path for path in actual_files_sorted if path not in expected_files]
    missing_commits = [oid for oid in expected_commits if oid not in actual_commits_set]
    unexpected_commits = [oid for oid in actual_commits if oid not in expected_commits_set]
    files_match = expected_files == actual_files_sorted
    commits_match = expected_commits == actual_commits
    base_match = payload.get("baseRefName") == expected.get("expected_base")
    head_match = payload.get("headRefName") == expected.get("expected_head")
    title_match = payload.get("title") == expected.get("expected_title")
    expected_is_draft = expected.get("expected_is_draft")
    if expected_is_draft is None:
        expected_is_draft = True
    draft_match = bool(payload.get("isDraft")) is bool(expected_is_draft)
    expected_state = str(expected.get("expected_state") or "OPEN").strip().upper()
    state_match = str(payload.get("state") or "").strip().upper() == expected_state
    passed = all(
        [
            files_match,
            commits_match,
            base_match,
            head_match,
            title_match,
            draft_match,
            state_match,
        ]
    )
    blocking_warnings: list[str] = []
    if not base_match:
        blocking_warnings.append("GitHub PR baseRefName does not match handoff base")
    if not head_match:
        blocking_warnings.append("GitHub PR headRefName does not match handoff head")
    if not draft_match:
        blocking_warnings.append(
            f"GitHub PR isDraft does not match expected {bool(expected_is_draft)}"
        )
    if not state_match:
        blocking_warnings.append(f"GitHub PR state is not {expected_state}")
    if not title_match:
        blocking_warnings.append("GitHub PR title does not match handoff title")
    if not files_match:
        blocking_warnings.append("GitHub PR files do not match handoff changed_files")
    if not commits_match:
        blocking_warnings.append("GitHub PR commits do not match expected branch diff")
    return {
        "performed": True,
        "passed": passed,
        "verified": passed,
        "actual_number": payload.get("number"),
        "actual_url": payload.get("url"),
        "expected_base": expected.get("expected_base"),
        "actual_base": payload.get("baseRefName"),
        "expected_head": expected.get("expected_head"),
        "actual_head": payload.get("headRefName"),
        "expected_title": expected.get("expected_title"),
        "actual_title": payload.get("title"),
        "expected_state": expected.get("expected_state"),
        "actual_state": payload.get("state"),
        "expected_is_draft": expected.get("expected_is_draft"),
        "actual_is_draft": payload.get("isDraft"),
        "expected_files": expected_files,
        "actual_files": actual_files_sorted,
        "missing_files": missing_files,
        "unexpected_files": unexpected_files,
        "expected_commits": expected_commits,
        "actual_commits": actual_commits,
        "missing_commits": missing_commits,
        "unexpected_commits": unexpected_commits,
        "files_match": files_match,
        "commits_match": commits_match,
        "base_match": base_match,
        "head_match": head_match,
        "title_match": title_match,
        "draft_match": draft_match,
        "state_match": state_match,
        "blocking_warnings": blocking_warnings,
    }
