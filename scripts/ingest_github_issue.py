#!/usr/bin/env python3
"""Mirror one GitHub Issue into the local Agent Taskflow DB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.github_issue_ingestion import (
    GitHubIssueIngestionError,
    GitHubIssueIngestionRequest,
    GitHubIssueSnapshot,
    ingest_github_issue,
    ingestion_result_to_dict,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.worktree import ensure_absolute_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only ingest of one GitHub Issue into the local task mirror.",
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form.")
    parser.add_argument("--issue-number", required=True, type=int, help="GitHub Issue number.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--local-repo-path",
        required=True,
        help="Absolute path to the local repository root for the task.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Absolute artifact root. Default: <local-repo-path>/.agent-taskflow/artifacts.",
    )
    parser.add_argument("--task-key", help="Optional explicit local task key.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and map the issue, but write no DB rows or artifacts.",
    )
    parser.add_argument(
        "--issue-json-path",
        help="Testing/offline input: read issue JSON from this absolute path instead of gh.",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_issue_json(path: Path) -> GitHubIssueSnapshot:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GitHubIssueIngestionError(f"could not read issue JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GitHubIssueIngestionError(f"invalid issue JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GitHubIssueIngestionError("issue JSON must be an object")
    try:
        return GitHubIssueSnapshot.from_json(data)
    except ValueError as exc:
        raise GitHubIssueIngestionError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        db_path = ensure_absolute_path(args.db_path, name="db_path") if args.db_path else None
        local_repo_path = ensure_absolute_path(args.local_repo_path, name="local_repo_path")
        artifact_root = (
            ensure_absolute_path(args.artifact_root, name="artifact_root")
            if args.artifact_root
            else None
        )
        issue_json_path = (
            ensure_absolute_path(args.issue_json_path, name="issue_json_path")
            if args.issue_json_path
            else None
        )
        request = GitHubIssueIngestionRequest(
            repo=args.repo,
            issue_number=args.issue_number,
            local_repo_path=local_repo_path,
            artifact_root=artifact_root,
            task_key=args.task_key,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        _emit({"ok": False, "status": "blocked", "summary": str(exc)})
        return 2

    fetcher = None
    if issue_json_path is not None:
        try:
            snapshot = _load_issue_json(issue_json_path)
        except GitHubIssueIngestionError as exc:
            _emit({"ok": False, "status": "blocked", "summary": str(exc)})
            return 1

        def fetcher(repo: str, issue_number: int) -> GitHubIssueSnapshot:
            _ = repo
            if issue_number != snapshot.number:
                raise GitHubIssueIngestionError(
                    f"issue JSON number {snapshot.number} does not match requested {issue_number}"
                )
            return snapshot

    try:
        store = TaskMirrorStore(db_path)
        if fetcher is None:
            result = ingest_github_issue(request, store=store)
        else:
            result = ingest_github_issue(request, store=store, fetcher=fetcher)
    except GitHubIssueIngestionError as exc:
        _emit({"ok": False, "status": "blocked", "summary": str(exc)})
        return 1
    except ValueError as exc:
        _emit({"ok": False, "status": "blocked", "summary": str(exc)})
        return 2

    _emit(ingestion_result_to_dict(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
