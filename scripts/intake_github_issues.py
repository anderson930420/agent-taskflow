#!/usr/bin/env python3
"""Intake explicitly selected GitHub Issues into the local task mirror."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot  # noqa: E402
from agent_taskflow.github_issue_intake_gate import (  # noqa: E402
    GitHubIssueIntakeError,
    GitHubIssueIntakeRequest,
    fetch_issue_with_gh,
    intake_selected_github_issues,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.worktree import ensure_absolute_path  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministically intake only explicitly selected GitHub Issues into SQLite.",
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form.")
    parser.add_argument(
        "--project",
        help="Project name for the mirrored task. Default: repo name.",
    )
    parser.add_argument(
        "--board",
        help="Board name for the mirrored task. Default: project.",
    )
    parser.add_argument(
        "--issue",
        "--issue-number",
        action="append",
        dest="issues",
        type=int,
        default=[],
        help="Selected GitHub Issue number. May be repeated.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the local repository root for the task.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Absolute path to the artifact root for task records.",
    )
    parser.add_argument(
        "--artifact-dir",
        help="Alias for --artifact-root.",
    )
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run the intake gate. This is the default when --confirm-intake is omitted.",
    )
    parser.add_argument(
        "--confirm-intake",
        action="store_true",
        help="Confirm writes to the SQLite task mirror.",
    )
    parser.add_argument(
        "--issues-json-path",
        help="Testing/offline input: read an issue JSON array from this path instead of gh.",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_issue_snapshots(path: Path) -> dict[int, GitHubIssueSnapshot]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GitHubIssueIntakeError(f"could not read issues JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GitHubIssueIntakeError(f"invalid issues JSON: {exc}") from exc
    if not isinstance(data, list):
        raise GitHubIssueIntakeError("issues JSON must be an array")

    snapshots: dict[int, GitHubIssueSnapshot] = {}
    for item in data:
        snapshot = GitHubIssueSnapshot.from_json(item)
        snapshots[snapshot.number] = snapshot
    return snapshots


def _issues_fetcher(
    request: GitHubIssueIntakeRequest,
    *,
    issue_json_path: Path | None,
) -> Callable[[str, int], GitHubIssueSnapshot]:
    if issue_json_path is None:
        return fetch_issue_with_gh

    snapshots = _load_issue_snapshots(issue_json_path)

    def fetcher(repo: str, issue_number: int) -> GitHubIssueSnapshot:
        _ = repo
        try:
            return snapshots[issue_number]
        except KeyError as exc:
            raise GitHubIssueIntakeError(
                f"issue JSON number {issue_number} was not provided in issues JSON"
            ) from exc

    return fetcher


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dry_run and args.confirm_intake:
        _emit(
            {
                "ok": False,
                "mode": "blocked",
                "selected": [],
                "written": False,
                "summary": "--dry-run and --confirm-intake are mutually exclusive",
            }
        )
        return 2

    artifact_root_arg = args.artifact_root or args.artifact_dir
    try:
        repo_path = ensure_absolute_path(args.repo_path, name="repo_path")
        if artifact_root_arg is None:
            raise ValueError("artifact_root must be provided via --artifact-root or --artifact-dir")
        artifact_root = ensure_absolute_path(artifact_root_arg, name="artifact_root")
        db_path = ensure_absolute_path(args.db_path, name="db_path") if args.db_path else None
        issue_json_path = (
            ensure_absolute_path(args.issues_json_path, name="issues_json_path")
            if args.issues_json_path
            else None
        )
        request = GitHubIssueIntakeRequest(
            repo=args.repo,
            issue_numbers=tuple(args.issues),
            repo_path=repo_path,
            artifact_root=artifact_root,
            project=args.project,
            board=args.board,
            db_path=db_path,
            dry_run=not args.confirm_intake,
        )
    except (ValueError, GitHubIssueIntakeError) as exc:
        _emit(
            {
                "ok": False,
                "mode": "blocked",
                "selected": [],
                "written": False,
                "summary": str(exc),
            }
        )
        return 1

    try:
        fetcher = _issues_fetcher(request, issue_json_path=issue_json_path)
    except GitHubIssueIntakeError as exc:
        _emit(
            {
                "ok": False,
                "mode": "blocked",
                "repo": request.repo,
                "project": request.project,
                "board": request.board,
                "repo_path": str(request.repo_path),
                "artifact_root": str(request.artifact_root),
                "selected": [],
                "written": False,
                "summary": str(exc),
            }
        )
        return 1

    try:
        store = TaskMirrorStore(request.db_path)
        payload = intake_selected_github_issues(request, store=store, fetcher=fetcher)
    except GitHubIssueIntakeError as exc:
        _emit(
            {
                "ok": False,
                "mode": "blocked",
                "repo": request.repo,
                "project": request.project,
                "board": request.board,
                "repo_path": str(request.repo_path),
                "artifact_root": str(request.artifact_root),
                "selected": [],
                "written": False,
                "summary": str(exc),
            }
        )
        return 1

    _emit(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
