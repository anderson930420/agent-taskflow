#!/usr/bin/env python3
"""Convert an eligible scheduler_proposal item into a scheduler_confirmation.

Dry-run by default. This command does NOT execute the proposed action.
It does NOT push, create PRs, merge, approve, reject, run cleanup,
mutate task status, contact GitHub, run any executor, run any
validator, call ``approved_task_runner``, create a verifier report,
create a handoff, run a scheduler loop, or start any background
worker. Mission Control is read-only and is not touched by this
command.

A scheduler confirmation produced here is auditable evidence for the
next gate only. It is NOT execution permission.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.scheduler_confirmation_from_proposal import (  # noqa: E402
    CONFIRMATION_SAFETY_FLAGS,
    SchedulerConfirmationFromProposalError,
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert an eligible scheduler_proposal item into a "
            "scheduler_confirmation artifact/event. Dry-run by default; "
            "never executes the proposed action; never mutates task status."
        ),
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Task key whose proposal item should be confirmed.",
    )
    parser.add_argument(
        "--proposal-item-id",
        required=True,
        help="Exact proposal_item_id from the stored scheduler_proposal artifact.",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Absolute path to the SQLite state DB.",
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        help=(
            "Absolute path under which scheduler_confirmations/<id>/ may "
            "be written."
        ),
    )

    parser.add_argument(
        "--proposal-hash",
        default=None,
        help="Optional proposal_hash bound at command time for stale-guard.",
    )
    parser.add_argument(
        "--proposal-id",
        default=None,
        help="Optional proposal_id bound at command time for stale-guard.",
    )
    parser.add_argument(
        "--item-hash",
        default=None,
        help="Optional item_hash bound at command time for stale-guard.",
    )
    parser.add_argument(
        "--recommended-command-kind",
        default=None,
        help="Optional recommended_command_kind bound at command time.",
    )
    parser.add_argument(
        "--expected-status",
        default=None,
        help="Optional task status expected at command time.",
    )
    parser.add_argument(
        "--proposal-artifact-path",
        default=None,
        help="Optional absolute path to the scheduler_proposal.json artifact.",
    )
    parser.add_argument(
        "--operator",
        default=None,
        help="Optional operator identifier recorded in the artifact/event.",
    )
    parser.add_argument(
        "--operator-note",
        default=None,
        help="Optional free-form operator note recorded in the artifact.",
    )
    parser.add_argument(
        "--confirm-create-confirmation",
        action="store_true",
        help=(
            "Persist the scheduler_confirmation JSON and record per-task "
            "scheduler_confirmation artifact/event. Without this flag the "
            "command runs dry-run and writes nothing. Does not execute the "
            "proposed action."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (default).",
    )
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit a human-readable summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dry_run = not args.confirm_create_confirmation

    try:
        request = SchedulerConfirmationFromProposalRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            task_key=args.task_key,
            proposal_item_id=args.proposal_item_id,
            proposal_hash=args.proposal_hash,
            proposal_id=args.proposal_id,
            item_hash=args.item_hash,
            recommended_command_kind=args.recommended_command_kind,
            expected_status=args.expected_status,
            proposal_artifact_path=(
                Path(args.proposal_artifact_path).expanduser()
                if args.proposal_artifact_path
                else None
            ),
            dry_run=dry_run,
            confirm_create_confirmation=bool(args.confirm_create_confirmation),
            operator=args.operator,
            operator_note=args.operator_note,
        )
        payload = create_scheduler_confirmation_from_proposal(request)
    except (ValueError, SchedulerConfirmationFromProposalError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(CONFIRMATION_SAFETY_FLAGS),
        }
        if args.pretty:
            print(f"error: {exc}", file=sys.stderr)
        else:
            print(json.dumps(error_payload, indent=2, sort_keys=True))
        return 1

    if args.pretty:
        print(_format_pretty(payload))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))

    if not payload.get("ok"):
        return 1
    return 0


def _format_pretty(payload: dict[str, Any]) -> str:
    confirmation = payload.get("confirmation") or {}
    eligibility = payload.get("eligibility") or {}
    safety = payload.get("safety") or {}

    lines = [
        "Scheduler Confirmation (from proposal)",
        f"  status:              {payload.get('status')}",
        f"  mode:                {payload.get('mode')}",
        f"  task_key:            {payload.get('task_key')}",
        f"  proposal_item_id:    {payload.get('proposal_item_id')}",
        f"  eligible:            {payload.get('eligible')}",
    ]
    if confirmation:
        lines.extend(
            [
                f"  confirmation_id:     {confirmation.get('confirmation_id')}",
                f"  proposal_id:         {confirmation.get('proposal_id')}",
                f"  proposal_hash:       {_short(confirmation.get('proposal_hash'))}",
                f"  item_hash:           {_short(confirmation.get('item_hash'))}",
                f"  recommended_kind:    "
                f"{confirmation.get('recommended_command_kind')}",
                f"  artifact_path:       "
                f"{confirmation.get('artifact_path') or '(dry-run; not written)'}",
            ]
        )
    if payload.get("status") == "not_eligible":
        lines.append("  reasons:")
        for reason in payload.get("reasons") or []:
            lines.append(f"    - {reason}")
    elif eligibility and eligibility.get("warnings"):
        lines.append("  warnings:")
        for warning in eligibility.get("warnings") or []:
            lines.append(f"    - {warning}")

    lines.append("  safety:")
    for key in sorted(safety.keys()):
        lines.append(f"    {key}: {safety[key]}")
    return "\n".join(lines)


def _short(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "(missing)"
    if len(value) <= 12:
        return value
    return value[:12] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
