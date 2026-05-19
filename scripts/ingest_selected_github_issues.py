#!/usr/bin/env python3
"""Intake selected GitHub Issues into the local task mirror."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.github_issue_intake import (  # noqa: E402
    GitHubIssueIntakeError,
    GitHubIssueIntakeRequest,
    GitHubIssueSnapshot,
    intake_result_to_dict,
    intake_selected_github_issues,
)
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot as IngestionIssueSnapshot  # noqa: E402
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.worktree import ensure_absolute_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Intake only the explicitly selected GitHub Issues into the local task mirror.",
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--local-repo-path",
        help="Absolute path to the local repository root for artifact placement. Defaults to the repo root.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Absolute artifact root. Default: <local-repo-path>/.agent-taskflow/artifacts.",
    )
    parser.add_argument(
        "--issue-number",
        action="append",
        type=int,
        default=[],
        help="Selected GitHub Issue number. May be repeated.",
    )
    parser.add_argument(
        "--issues",
        help="Comma-separated selected GitHub Issue numbers.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and classify the selected issues, but write no DB rows or artifacts.",
    )
    parser.add_argument(
        "--issues-json-path",
        help="Testing/offline input: read a GitHub issue JSON array from this path instead of gh.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON. This is the only output format and is enabled by default.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output. This is the default for operator readability.",
    )
    return parser


def _emit(payload: dict[str, object], *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _parse_issue_numbers(values: list[int], issues_value: str | None) -> tuple[int, ...]:
    numbers = list(values)
    if issues_value:
        for raw in issues_value.split(","):
            value = raw.strip()
            if not value:
                continue
            try:
                numbers.append(int(value))
            except ValueError as exc:
                raise ValueError(f"invalid issue number in --issues: {value!r}") from exc
    return tuple(numbers)


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
        snapshot = IngestionIssueSnapshot.from_json(item)
        snapshots[snapshot.number] = snapshot
    return snapshots


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        issue_numbers = _parse_issue_numbers(args.issue_number, args.issues)
        local_repo_path = (
            ensure_absolute_path(args.local_repo_path, name="local_repo_path")
            if args.local_repo_path
            else REPO_ROOT
        )
        artifact_root = (
            ensure_absolute_path(args.artifact_root, name="artifact_root")
            if args.artifact_root
            else None
        )
        request = GitHubIssueIntakeRequest(
            repo=args.repo,
            issue_numbers=issue_numbers,
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            local_repo_path=local_repo_path,
            artifact_root=artifact_root,
            dry_run=args.dry_run,
        )
    except (ValueError, GitHubIssueIntakeError) as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "summary": str(exc),
                "safety": {
                    "read_only": args.dry_run if hasattr(args, "dry_run") else False,
                    "selected_intake_only": True,
                    "db_written": False,
                    "artifact_written": False,
                    "event_recorded": False,
                    "workspace_prepared": False,
                    "executor_started": False,
                    "validators_started": False,
                    "branch_pushed": False,
                    "pr_created": False,
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                },
            }
        )
        return 2 if isinstance(exc, ValueError) else 1

    issue_json_path = Path(args.issues_json_path).expanduser() if args.issues_json_path else None
    snapshots = _load_issue_snapshots(issue_json_path) if issue_json_path else None

    def fetcher(repo: str, issue_number: int) -> GitHubIssueSnapshot:
        _ = repo
        if snapshots is None:
            from agent_taskflow.github_issue_intake import fetch_selected_issue_with_gh

            return fetch_selected_issue_with_gh(request.repo, issue_number)
        if issue_number not in snapshots:
            raise GitHubIssueIntakeError(
                f"issue JSON number {issue_number} was not provided in issues JSON"
            )
        return snapshots[issue_number]

    try:
        store = TaskMirrorStore(request.db_path)
        result = intake_selected_github_issues(request, store=store, fetcher=fetcher)
    except GitHubIssueIntakeError as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "summary": str(exc),
                "safety": {
                    "read_only": args.dry_run,
                    "selected_intake_only": True,
                    "db_written": False,
                    "artifact_written": False,
                    "event_recorded": False,
                    "workspace_prepared": False,
                    "executor_started": False,
                    "validators_started": False,
                    "branch_pushed": False,
                    "pr_created": False,
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                },
            }
        )
        return 1

    _emit(intake_result_to_dict(result), compact=args.json and not args.pretty)
    return 0 if result["status"] != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
