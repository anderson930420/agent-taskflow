#!/usr/bin/env python3
"""Create Level 5A intake_runner_handoff evidence.

Dry-run by default. This command does not start runtime execution, does
not call the approved task runner, does not invoke executors or
validators, does not mutate GitHub, and does not approve, merge, clean
up, run a scheduler loop, start any background worker, or automatically
pick tasks.
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

from agent_taskflow.intake_runner_handoff_from_verifier_report import (  # noqa: E402
    HANDOFF_SAFETY_FLAGS,
    IntakeRunnerHandoffFromVerifierReportError,
    IntakeRunnerHandoffFromVerifierReportRequest,
    create_intake_runner_handoff_from_verifier_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create intake_runner_handoff evidence from an existing "
            "scheduler_confirmation_verifier_report. Dry-run by default; "
            "never executes workflow actions."
        ),
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Task key whose verifier report should be handed off.",
    )
    parser.add_argument(
        "--verifier-report-id",
        required=True,
        help="Exact verifier_report_id from scheduler verifier evidence.",
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
            "Absolute path under which intake_runner_handoffs/<id>/ may be "
            "written."
        ),
    )

    parser.add_argument("--confirmation-id", default=None)
    parser.add_argument("--proposal-hash", default=None)
    parser.add_argument("--proposal-item-id", default=None)
    parser.add_argument("--item-hash", default=None)
    parser.add_argument("--recommended-command-kind", default=None)
    parser.add_argument(
        "--verifier-report-artifact-path",
        default=None,
        help="Optional path to the scheduler_confirmation_verifier_report JSON.",
    )
    parser.add_argument(
        "--operator",
        default=None,
        help="Optional operator identifier recorded in the handoff artifact.",
    )
    parser.add_argument(
        "--operator-note",
        default=None,
        help="Optional free-form operator note recorded in the handoff artifact.",
    )
    parser.add_argument(
        "--confirm-create-handoff",
        action="store_true",
        help=(
            "Persist the intake_runner_handoff artifact/event. Without this "
            "flag the command runs dry-run and writes nothing."
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
    dry_run = not args.confirm_create_handoff

    try:
        request = IntakeRunnerHandoffFromVerifierReportRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            task_key=args.task_key,
            verifier_report_id=args.verifier_report_id,
            confirmation_id=args.confirmation_id,
            proposal_hash=args.proposal_hash,
            proposal_item_id=args.proposal_item_id,
            item_hash=args.item_hash,
            recommended_command_kind=args.recommended_command_kind,
            verifier_report_artifact_path=(
                Path(args.verifier_report_artifact_path).expanduser()
                if args.verifier_report_artifact_path
                else None
            ),
            dry_run=dry_run,
            confirm_create_handoff=bool(args.confirm_create_handoff),
            operator=args.operator,
            operator_note=args.operator_note,
        )
        payload = create_intake_runner_handoff_from_verifier_report(request)
    except (ValueError, IntakeRunnerHandoffFromVerifierReportError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(HANDOFF_SAFETY_FLAGS),
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
    handoff = payload.get("handoff") or {}
    binding = payload.get("binding") or {}
    verifier = binding.get("verifier_report") or {}
    safety = payload.get("safety") or {}

    lines = [
        "Intake Runner Handoff",
        f"  status:                 {payload.get('status')}",
        f"  mode:                   {payload.get('mode')}",
        f"  task_key:               {binding.get('task_key')}",
        f"  verifier_report_id:     {verifier.get('verifier_report_id')}",
        f"  handoff_allowed:        {binding.get('handoff_allowed')}",
    ]
    if handoff:
        lines.extend(
            [
                f"  handoff_id:             {handoff.get('handoff_id')}",
                f"  confirmation_id:        {handoff.get('confirmation_id')}",
                f"  proposal_hash:          {_short(handoff.get('proposal_hash'))}",
                f"  proposal_item_id:       {handoff.get('proposal_item_id')}",
                f"  item_hash:              {_short(handoff.get('item_hash'))}",
                f"  recommended_kind:       {handoff.get('recommended_command_kind')}",
                f"  artifact_path:          {handoff.get('artifact_path')}",
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
