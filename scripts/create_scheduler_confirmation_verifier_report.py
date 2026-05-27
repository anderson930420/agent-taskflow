#!/usr/bin/env python3
"""Create a scheduler confirmation verifier report artifact/event.

Dry-run by default. This command does not create a handoff, does not
start runtime execution, does not call the approved task runner, does
not invoke executors or validators, does not mutate GitHub, and does
not approve, merge, clean up, run a scheduler loop, or start any
background worker.
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

from agent_taskflow.scheduler_confirmation_verifier_report import (  # noqa: E402
    VERIFIER_REPORT_SAFETY_FLAGS,
    SchedulerConfirmationVerifierReportError,
    SchedulerConfirmationVerifierReportRequest,
    create_scheduler_confirmation_verifier_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create scheduler_confirmation_verifier_report evidence for an "
            "existing scheduler_confirmation. Dry-run by default; never "
            "executes workflow actions."
        ),
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Task key whose scheduler_confirmation should be verified.",
    )
    parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Exact confirmation_id from scheduler_confirmation evidence.",
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
            "Absolute path under which scheduler_confirmation_verifier_reports/"
            "<id>/ may be written."
        ),
    )

    parser.add_argument(
        "--proposal-hash",
        default=None,
        help="Optional proposal_hash binding guard.",
    )
    parser.add_argument(
        "--proposal-item-id",
        default=None,
        help="Optional proposal_item_id binding guard.",
    )
    parser.add_argument(
        "--item-hash",
        default=None,
        help="Optional item_hash binding guard.",
    )
    parser.add_argument(
        "--recommended-command-kind",
        default=None,
        help="Optional recommended_command_kind binding guard.",
    )
    parser.add_argument(
        "--confirmation-artifact-path",
        default=None,
        help="Optional path to the scheduler_confirmation.json artifact.",
    )
    parser.add_argument(
        "--operator",
        default=None,
        help="Optional operator identifier recorded in the report artifact.",
    )
    parser.add_argument(
        "--operator-note",
        default=None,
        help="Optional free-form operator note recorded in the report artifact.",
    )
    parser.add_argument(
        "--confirm-create-verifier-report",
        action="store_true",
        help=(
            "Persist the scheduler_confirmation_verifier_report artifact/event. "
            "Without this flag the command runs dry-run and writes nothing."
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
    dry_run = not args.confirm_create_verifier_report

    try:
        request = SchedulerConfirmationVerifierReportRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            task_key=args.task_key,
            confirmation_id=args.confirmation_id,
            proposal_hash=args.proposal_hash,
            proposal_item_id=args.proposal_item_id,
            item_hash=args.item_hash,
            recommended_command_kind=args.recommended_command_kind,
            confirmation_artifact_path=(
                Path(args.confirmation_artifact_path).expanduser()
                if args.confirmation_artifact_path
                else None
            ),
            dry_run=dry_run,
            confirm_create_verifier_report=bool(
                args.confirm_create_verifier_report
            ),
            operator=args.operator,
            operator_note=args.operator_note,
        )
        payload = create_scheduler_confirmation_verifier_report(request)
    except (ValueError, SchedulerConfirmationVerifierReportError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(VERIFIER_REPORT_SAFETY_FLAGS),
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
    report = payload.get("verifier_report") or {}
    binding = payload.get("binding") or {}
    confirmation = binding.get("confirmation") or {}
    safety = payload.get("safety") or {}

    lines = [
        "Scheduler Confirmation Verifier Report",
        f"  status:                 {payload.get('status')}",
        f"  mode:                   {payload.get('mode')}",
        f"  task_key:               {confirmation.get('task_key') or binding.get('task_key')}",
        f"  confirmation_id:        {confirmation.get('confirmation_id')}",
        f"  verification_passed:    {payload.get('verification_passed')}",
    ]
    if report:
        lines.extend(
            [
                f"  verifier_report_id:     {report.get('verifier_report_id')}",
                f"  proposal_hash:          {_short(report.get('proposal_hash'))}",
                f"  proposal_item_id:       {report.get('proposal_item_id')}",
                f"  item_hash:              {_short(report.get('item_hash'))}",
                f"  recommended_kind:       {report.get('recommended_command_kind')}",
                f"  artifact_path:          {report.get('artifact_path')}",
            ]
        )
    if payload.get("reasons"):
        lines.append("  reasons:")
        for reason in payload.get("reasons") or []:
            lines.append(f"    - {reason}")
    elif binding.get("warnings"):
        lines.append("  warnings:")
        for warning in binding.get("warnings") or []:
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
    return value[:12] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
