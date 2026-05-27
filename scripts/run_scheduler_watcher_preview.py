#!/usr/bin/env python3
"""Run the Level 8A scheduler watcher dry-run preview."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.scheduler_watcher_preview import (  # noqa: E402
    SchedulerWatcherPreviewError,
    SchedulerWatcherPreviewRequest,
    WATCHER_PREVIEW_SAFETY_FLAGS,
    WATCHER_PREVIEW_SCHEMA_VERSION,
    WATCHER_PREVIEW_SOURCE,
    build_scheduler_watcher_preview,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview scheduler watcher candidates in dry-run mode. This is "
            "read-only and does not run tasks, push branches, or create PRs."
        )
    )
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project")
    parser.add_argument("--status")
    parser.add_argument("--recommended-command-kind")
    parser.add_argument("--include-blocked", action="store_true")
    parser.add_argument("--include-waiting-approval", action="store_true")
    parser.add_argument("--include-completed", action="store_true")
    parser.add_argument("--include-no-action", action="store_true")
    parser.add_argument("--operator")
    parser.add_argument("--operator-note")

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = SchedulerWatcherPreviewRequest(
            db_path=Path(args.db_path).expanduser(),
            limit=int(args.limit),
            project=args.project,
            status=args.status,
            recommended_command_kind=args.recommended_command_kind,
            include_blocked=bool(args.include_blocked),
            include_waiting_approval=bool(args.include_waiting_approval),
            include_completed=bool(args.include_completed),
            include_no_action=bool(args.include_no_action),
            operator=args.operator,
            operator_note=args.operator_note,
        )
        payload = build_scheduler_watcher_preview(request)
    except (ValueError, SchedulerWatcherPreviewError) as exc:
        payload = {
            "ok": False,
            "schema_version": WATCHER_PREVIEW_SCHEMA_VERSION,
            "source": WATCHER_PREVIEW_SOURCE,
            "mode": "dry_run_preview",
            "status": "error",
            "error": str(exc),
            "candidate_count": 0,
            "skipped_count": 0,
            "candidates": [],
            "skipped": [],
            "safety": dict(WATCHER_PREVIEW_SAFETY_FLAGS),
        }
        _emit(payload, pretty=bool(args.pretty))
        return 1

    _emit(payload, pretty=bool(args.pretty))
    return 0


def _emit(payload: dict[str, object], *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
