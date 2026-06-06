#!/usr/bin/env python3
"""Summarize the real scheduled execution path for operators (read-only).

This command reads an existing JSONL scheduler tick log (Level 10H cron output)
and the local task mirror, then summarizes the latest tick, recent tick counts,
the backlog, and the ingestion failure registry. It is read-only observability:
it never modifies crontab, writes the database, calls GitHub, runs an executor
or validator, ingests an issue, pushes, creates a PR, merges, approves, cleans
up, deletes a branch/worktree, or starts a daemon, scheduler loop, webhook, or
background worker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.real_scheduled_execution_observability import (  # noqa: E402
    REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SCHEMA_VERSION,
    REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SOURCE,
    DEFAULT_RECENT_LIMIT,
    RealScheduledExecutionObservabilityRequest,
    observability_safety_flags,
    render_real_scheduled_execution_summary,
    summarize_real_scheduled_execution,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only observability for the real scheduled execution path. "
            "Summarizes the JSONL scheduler tick log and local task mirror. "
            "It does not trigger any automation."
        )
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--log-path",
        default=None,
        help="Path to the JSONL scheduler tick log written by the cron tick.",
    )
    parser.add_argument(
        "--recent-limit",
        type=int,
        default=DEFAULT_RECENT_LIMIT,
        help=(
            "How many of the most recent ticks/records to summarize. "
            f"Default: {DEFAULT_RECENT_LIMIT}."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. Without this flag the output is human-readable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = RealScheduledExecutionObservabilityRequest(
            log_path=Path(args.log_path).expanduser() if args.log_path else None,
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            recent_limit=int(args.recent_limit),
        )
        summary = summarize_real_scheduled_execution(request)
    except (ValueError, OSError) as exc:
        payload = _error_payload(
            str(exc),
            log_path=args.log_path,
            db_path=args.db_path,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Real scheduled execution observability error: {exc}")
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_real_scheduled_execution_summary(summary), end="")

    return 0 if summary.get("ok") else 1


def _error_payload(
    message: str,
    *,
    log_path: str | None,
    db_path: str | None,
) -> dict[str, object]:
    return {
        "ok": False,
        "schema_version": REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SCHEMA_VERSION,
        "source": REAL_SCHEDULED_EXECUTION_OBSERVABILITY_SOURCE,
        "log_path": log_path,
        "db_path": db_path,
        "last_tick": None,
        "recent_ticks": None,
        "backlog": None,
        "ingestion_failure_registry": None,
        "warnings": [message],
        "safety": observability_safety_flags(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
