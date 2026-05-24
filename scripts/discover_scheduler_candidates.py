#!/usr/bin/env python3
"""List read-only scheduler candidates (Phase G — Level 1 discovery).

This command reads the live task mirror and returns the tasks that are
currently candidates for the scheduler flow. It is strictly read-only.

It does NOT:

- write to the SQLite mirror
- write any artifacts
- create scheduler proposals
- create scheduler confirmations
- create verifier reports
- create intake_runner_handoff artifacts
- run queued_task_handoff or any runtime execution
- invoke approved_task_runner
- mutate GitHub (no push, no PR, no merge, no comment)
- approve, merge, or perform cleanup
- start any background worker or scheduler loop

Being listed by this command is NOT execution permission. Human/operator
confirmation remains required; validation_result remains authoritative;
Mission Control remains read-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.scheduler_candidate_discovery import (  # noqa: E402
    DISCOVERY_SAFETY_FLAGS,
    SchedulerCandidateDiscoveryError,
    SchedulerCandidateDiscoveryRequest,
    discover_scheduler_candidates,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List read-only scheduler candidates. This is Level 1 discovery "
            "only: no proposal write, no confirmation, no handoff, no "
            "runtime execution, no approved_task_runner, no GitHub mutation."
        ),
    )
    parser.add_argument(
        "--db-path",
        help=(
            "Path to the Agent Taskflow SQLite state DB. Defaults to "
            "~/.agent-taskflow/state.db."
        ),
    )
    parser.add_argument("--task-key", help="Optional task key filter.")
    parser.add_argument("--project", help="Optional project filter.")
    parser.add_argument("--status", help="Optional task status filter.")
    parser.add_argument(
        "--include-not-ready",
        action="store_true",
        help=(
            "Include candidates whose recommended kind is unknown or "
            "human_pr_review (these are not scheduler-actionable). "
            "Does not include no_action. "
            "Excluded by default."
        ),
    )
    parser.add_argument(
        "--include-no-action",
        action="store_true",
        help=(
            "Include candidates whose recommended kind is no_action. "
            "Excluded by default."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of candidates to return.",
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="Emit compact JSON (default).",
    )
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit pretty (indented) JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = SchedulerCandidateDiscoveryRequest(
            db_path=args.db_path,
            task_key=args.task_key,
            project=args.project,
            status=args.status,
            include_not_ready=args.include_not_ready,
            include_no_action=args.include_no_action,
            limit=args.limit,
        )
        payload = discover_scheduler_candidates(request)
    except (ValueError, SchedulerCandidateDiscoveryError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "candidate_count": 0,
            "candidates": [],
            "safety": dict(DISCOVERY_SAFETY_FLAGS),
        }
        _emit(error_payload, pretty=args.pretty)
        return 1

    _emit(payload, pretty=args.pretty)
    return 0


def _emit(payload: dict[str, object], *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
