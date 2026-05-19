#!/usr/bin/env python3
"""Discover GitHub Issues eligible for later local intake."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.github_issue_discovery import (  # noqa: E402
    GitHubIssueDiscoveryError,
    GitHubIssueDiscoveryIssue,
    GitHubIssueDiscoveryRequest,
    discover_github_issues,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover GitHub Issues without ingesting or mutating local state.",
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form.")
    parser.add_argument(
        "--db-path",
        help="Path to the Agent Taskflow SQLite state DB. Defaults to ~/.agent-taskflow/state.db.",
    )
    parser.add_argument(
        "--state",
        choices=("open", "closed", "all"),
        default="open",
        help="GitHub Issue state to read. Default: open.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of issues to read from GitHub. Default: 100.",
    )
    parser.add_argument(
        "--include-label",
        action="append",
        default=[],
        help="Require this label before recommending an issue. May be repeated.",
    )
    parser.add_argument(
        "--exclude-label",
        action="append",
        default=[],
        help="Exclude this label from recommendations. May be repeated.",
    )
    parser.add_argument(
        "--issues-json-path",
        help="Testing/offline input: read an issue-list JSON array from this path instead of gh.",
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


def _load_issues_json(path: Path) -> list[GitHubIssueDiscoveryIssue]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GitHubIssueDiscoveryError(f"could not read issues JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GitHubIssueDiscoveryError(f"invalid issues JSON: {exc}") from exc
    if not isinstance(data, list):
        raise GitHubIssueDiscoveryError("issues JSON must be an array")
    try:
        return [GitHubIssueDiscoveryIssue.from_json(item) for item in data]
    except (TypeError, ValueError) as exc:
        raise GitHubIssueDiscoveryError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = GitHubIssueDiscoveryRequest(
            repo=args.repo,
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            state=args.state,
            limit=args.limit,
            include_labels=tuple(args.include_label),
            exclude_labels=tuple(args.exclude_label),
        )
        issues_json_path = Path(args.issues_json_path).expanduser() if args.issues_json_path else None
        issues = _load_issues_json(issues_json_path) if issues_json_path else None

        if issues is None:
            payload = discover_github_issues(request)
        else:
            payload = discover_github_issues(request, fetcher=lambda _: issues)
    except (ValueError, GitHubIssueDiscoveryError) as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "summary": str(exc),
                "safety": {
                    "read_only": True,
                    "ingested": False,
                    "db_written": False,
                    "workspace_prepared": False,
                    "executor_started": False,
                    "branch_pushed": False,
                    "pr_created": False,
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                },
            }
        )
        return 1

    _emit(payload, compact=args.json and not args.pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
