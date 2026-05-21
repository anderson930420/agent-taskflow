#!/usr/bin/env python3
"""Produce a read-only scheduler proposal from task recommendations.

Dry-run by default. This command does NOT implement a scheduler, NOT a
background loop, and NOT a polling daemon. It does NOT execute any proposed
action: no executor, no validator, no push, no PR, no merge, no approval,
no cleanup, no GitHub mutation.

A scheduler proposal is never action evidence. With
`--confirm-create-proposal` the command writes a proposal JSON file and
records per-task ``scheduler_proposal`` artifact and
``scheduler_proposal_created`` event entries that are intentionally disjoint
from the workflow's action evidence types. They must not be interpreted as
proof that any proposed action ran.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.scheduler_proposals import (  # noqa: E402
    DEFAULT_MAX_ITEMS,
    PROPOSAL_SAFETY_FLAGS,
    SchedulerProposalError,
    SchedulerProposalRequest,
    create_scheduler_proposal,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Produce a scheduler proposal from task recommendations. "
            "Read-only by default. This is not a scheduler and does not "
            "execute workflow actions."
        ),
    )
    parser.add_argument("--db-path", required=True, help="SQLite state DB path.")
    parser.add_argument(
        "--artifact-root",
        required=True,
        help="Absolute path under which scheduler_proposals/<id>/ may be written.",
    )
    parser.add_argument("--status", help="Optional recommendation status filter.")
    parser.add_argument("--project", help="Optional project filter.")
    parser.add_argument("--task-key", help="Optional task key filter.")
    parser.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        help=f"Maximum items to include (default: {DEFAULT_MAX_ITEMS}).",
    )
    parser.add_argument(
        "--include-completed",
        action="store_true",
        help="Also consider completed tasks (otherwise excluded).",
    )
    parser.add_argument(
        "--include-no-action",
        action="store_true",
        help="Also include items whose recommended kind is no_action.",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="Include items whose recommended kind is unknown.",
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit a human-readable summary (default).",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the proposal only; never write to disk or DB (default).",
    )
    mode.add_argument(
        "--confirm-create-proposal",
        action="store_true",
        help=(
            "Persist the proposal JSON to <artifact-root>/scheduler_proposals/"
            "<proposal_id>/scheduler_proposal.json and record per-task "
            "scheduler_proposal evidence."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Default behavior is dry-run. --dry-run is allowed but ignored if also
    # given alongside the parser's default; --confirm-create-proposal flips
    # the mode.
    dry_run = not args.confirm_create_proposal

    try:
        request = SchedulerProposalRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            status=args.status,
            project=args.project,
            task_key=args.task_key,
            include_completed=args.include_completed,
            include_no_action=args.include_no_action,
            include_unknown=args.include_unknown,
            max_items=args.max_items,
            dry_run=dry_run,
            confirm_create_proposal=args.confirm_create_proposal,
        )
        payload = create_scheduler_proposal(request)
    except (ValueError, SchedulerProposalError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(PROPOSAL_SAFETY_FLAGS),
        }
        if args.json:
            print(json.dumps(error_payload, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_pretty(payload))
    return 0


def _format_pretty(payload: dict[str, object]) -> str:
    lines = [
        "Scheduler Proposal",
        f"  proposal_id:        {payload.get('proposal_id')}",
        f"  schema_version:     {payload.get('schema_version')}",
        f"  mode:               {payload.get('mode')}",
        f"  created_at:         {payload.get('created_at')}",
        f"  db_path:            {payload.get('db_path')}",
        f"  artifact_root:      {payload.get('artifact_root')}",
        f"  artifact_path:      {payload.get('artifact_path') or '(dry-run; not written)'}",
    ]

    summary = payload.get("summary")
    if isinstance(summary, dict):
        lines.extend(
            [
                f"  item_count:         {summary.get('item_count')}",
                f"  candidate_count:    {summary.get('candidate_count')}",
                f"  executable_count:   {summary.get('executable_count')}",
                f"  warning_count:      {summary.get('warning_count')}",
                f"  evidence_recorded:  {summary.get('proposal_evidence_recorded')}",
            ]
        )

    items = payload.get("items")
    if isinstance(items, list) and items:
        lines.append("")
        lines.append("Items:")
        for item in items:
            if not isinstance(item, dict):
                continue
            executable = "executable" if item.get("executable") else "needs-inspection"
            lines.append("")
            lines.append(
                f"  {item.get('task_key')} [{item.get('status')}]"
                f" — {item.get('recommended_command_kind')}"
                f" ({executable})"
            )
            lines.append(
                f"    Phase: {item.get('current_phase_label')} | "
                f"severity: {item.get('severity')} | "
                f"confidence: {item.get('confidence')}"
            )
            lines.append(f"    Proposed action: {item.get('proposed_action')}")
            lines.append(f"    Reason: {item.get('reason')}")
            warnings = item.get("consistency_warnings")
            if isinstance(warnings, list) and warnings:
                lines.append("    Warnings:")
                for warning in warnings:
                    lines.append(f"      - {warning}")
    else:
        lines.append("")
        lines.append("Items: (none)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
