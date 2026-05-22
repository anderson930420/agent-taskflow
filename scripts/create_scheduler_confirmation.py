#!/usr/bin/env python3
"""Record an operator pre-approval (confirmation) for scheduler proposal items.

Dry-run by default. This command does NOT execute any proposed action.
It does NOT push, create PRs, merge, approve, reject, run cleanup,
mutate task status, contact GitHub, or start any background worker.

A scheduler confirmation artifact is intentionally NOT action evidence.
Existing command-specific ``--confirm-*`` helpers remain the only
mutation gates. The confirmation artifact is audit/pre-approval only.
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

from agent_taskflow.scheduler_confirmations import (  # noqa: E402
    CONFIRMATION_SAFETY_FLAGS,
    SchedulerConfirmationError,
    SchedulerConfirmationRequest,
    create_scheduler_confirmation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record a scheduler confirmation (pre-approval) artifact for one "
            "or more hash-bound proposal items. Read-only by default; never "
            "executes proposed actions; never mutates task status."
        ),
    )
    parser.add_argument("--db-path", required=True, help="SQLite state DB path.")
    parser.add_argument(
        "--artifact-root",
        required=True,
        help=(
            "Absolute path under which scheduler_confirmations/<id>/ may "
            "be written."
        ),
    )

    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--proposal-id",
        help="Confirm items from the proposal with this proposal_id.",
    )
    selector.add_argument(
        "--proposal-artifact-path",
        help="Confirm items from the scheduler_proposal.json at this path.",
    )
    selector.add_argument(
        "--latest",
        action="store_true",
        help="Confirm items from the most recently recorded proposal.",
    )

    parser.add_argument(
        "--item-id",
        action="append",
        default=[],
        help=(
            "Exact proposal_item_id to confirm. May be passed multiple times "
            "or comma-separated."
        ),
    )
    parser.add_argument(
        "--acknowledge-warnings",
        action="store_true",
        help=(
            "Acknowledge consistency_warnings on selected items. Still does "
            "not allow execution."
        ),
    )
    parser.add_argument(
        "--confirmed-by",
        help="Optional operator identifier recorded in the artifact.",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the confirmation only; never write to disk or DB (default).",
    )
    mode.add_argument(
        "--confirm-create-confirmation",
        action="store_true",
        help=(
            "Persist the confirmation JSON to <artifact-root>/"
            "scheduler_confirmations/<confirmation_id>/scheduler_confirmation.json "
            "and record per-task scheduler_confirmation evidence. Does not "
            "execute the proposed action."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit a human-readable summary (default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dry_run = not args.confirm_create_confirmation

    item_ids: list[str] = []
    for raw in args.item_id or []:
        for piece in raw.split(","):
            piece = piece.strip()
            if piece and piece not in item_ids:
                item_ids.append(piece)

    try:
        request = SchedulerConfirmationRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            proposal_id=args.proposal_id,
            proposal_artifact_path=(
                Path(args.proposal_artifact_path).expanduser()
                if args.proposal_artifact_path
                else None
            ),
            latest=bool(args.latest),
            selected_item_ids=tuple(item_ids),
            acknowledge_warnings=bool(args.acknowledge_warnings),
            dry_run=dry_run,
            confirm_create_confirmation=bool(args.confirm_create_confirmation),
            confirmed_by=args.confirmed_by,
        )
        payload = create_scheduler_confirmation(request)
    except (ValueError, SchedulerConfirmationError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(CONFIRMATION_SAFETY_FLAGS),
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


def _format_pretty(payload: dict[str, Any]) -> str:
    proposal = payload.get("proposal") or {}
    summary = payload.get("summary") or {}
    items = payload.get("selected_items") or []
    proposal_hash = proposal.get("proposal_hash") or ""
    proposal_hash_short = (
        proposal_hash[:12] + "…" if isinstance(proposal_hash, str) and proposal_hash
        else "(missing)"
    )
    lines = [
        "Scheduler Confirmation",
        f"  confirmation_id:    {payload.get('confirmation_id')}",
        f"  schema_version:     {payload.get('schema_version')}",
        f"  mode:               {payload.get('mode')}",
        f"  created_at:         {payload.get('created_at')}",
        f"  confirmed_by:       {payload.get('confirmed_by')}",
        f"  artifact_path:      {payload.get('artifact_path') or '(dry-run; not written)'}",
        f"  proposal_id:        {proposal.get('proposal_id')}",
        f"  proposal_hash:      {proposal_hash_short}",
        f"  selected_count:     {summary.get('selected_item_count')}",
        f"  warning_count:      {summary.get('warning_count')}",
        f"  execution_allowed:  {summary.get('execution_allowed')}",
        f"  evidence_recorded:  {summary.get('confirmation_evidence_recorded')}",
    ]
    if items:
        lines.append("")
        lines.append("Selected items:")
        for item in items:
            item_hash = item.get("item_hash") or ""
            item_hash_short = item_hash[:12] + "…" if item_hash else "(missing)"
            lines.append("")
            lines.append(
                f"  {item.get('proposal_item_id')} "
                f"[{item.get('task_key')}] "
                f"— {item.get('recommended_command_kind')}"
            )
            lines.append(f"    item_hash:                       {item_hash_short}")
            lines.append(
                f"    expected_status:                 {item.get('expected_status')}"
            )
            lines.append(
                f"    expected_phase_label:            {item.get('expected_phase_label')}"
            )
            lines.append(
                f"    operator_acknowledged_warnings:  "
                f"{item.get('operator_acknowledged_warnings')}"
            )
            lines.append(
                f"    revalidation_required:           "
                f"{item.get('revalidation_required')}"
            )
            lines.append(
                f"    execution_allowed:               "
                f"{item.get('execution_allowed')}"
            )
            warnings = item.get("consistency_warnings") or []
            if warnings:
                lines.append("    consistency_warnings:")
                for warning in warnings:
                    lines.append(f"      - {warning}")
    else:
        lines.append("")
        lines.append("Selected items: (none)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
