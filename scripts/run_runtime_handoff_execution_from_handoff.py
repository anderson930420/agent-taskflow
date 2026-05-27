#!/usr/bin/env python3
"""Run the Level 6A runtime handoff execution path.

Dry-run by default. With --confirm-run-approved-task-runner the command
runs runtime preflight against an existing intake_runner_handoff and, if
preflight passes, calls approved_task_runner exactly once. It does not
start a scheduler loop, a background worker, or automatic task picking,
and does not approve, merge, clean up, or mutate GitHub outside of
approved_task_runner's own explicit responsibility.
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

from agent_taskflow.runtime_handoff_execution_from_handoff import (  # noqa: E402
    RUNTIME_EXECUTION_SAFETY_FLAGS,
    RuntimeHandoffExecutionError,
    RuntimeHandoffExecutionRequest,
    run_runtime_handoff_execution_from_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run runtime preflight against an intake_runner_handoff and, "
            "with --confirm-run-approved-task-runner, invoke "
            "approved_task_runner exactly once. Dry-run by default."
        ),
    )
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--handoff-id", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--artifact-root", required=True)

    parser.add_argument("--verifier-report-id", default=None)
    parser.add_argument("--confirmation-id", default=None)
    parser.add_argument("--proposal-hash", default=None)
    parser.add_argument("--proposal-item-id", default=None)
    parser.add_argument("--item-hash", default=None)
    parser.add_argument("--recommended-command-kind", default=None)
    parser.add_argument("--handoff-artifact-path", default=None)
    parser.add_argument("--operator", default=None)
    parser.add_argument("--operator-note", default=None)
    parser.add_argument(
        "--confirm-run-approved-task-runner",
        action="store_true",
        help=(
            "Confirm runtime execution. Without this flag the command "
            "runs preflight in dry-run and never calls approved_task_runner."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry_run = not args.confirm_run_approved_task_runner

    try:
        request = RuntimeHandoffExecutionRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            task_key=args.task_key,
            handoff_id=args.handoff_id,
            verifier_report_id=args.verifier_report_id,
            confirmation_id=args.confirmation_id,
            proposal_hash=args.proposal_hash,
            proposal_item_id=args.proposal_item_id,
            item_hash=args.item_hash,
            recommended_command_kind=args.recommended_command_kind,
            handoff_artifact_path=(
                Path(args.handoff_artifact_path).expanduser()
                if args.handoff_artifact_path
                else None
            ),
            dry_run=dry_run,
            confirm_run_approved_task_runner=bool(
                args.confirm_run_approved_task_runner
            ),
            operator=args.operator,
            operator_note=args.operator_note,
        )
        payload = run_runtime_handoff_execution_from_handoff(request)
    except (ValueError, RuntimeHandoffExecutionError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(RUNTIME_EXECUTION_SAFETY_FLAGS),
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

    if payload.get("ok"):
        return 0
    return 1


def _format_pretty(payload: dict[str, Any]) -> str:
    preflight = payload.get("preflight") or {}
    handoff = preflight.get("handoff") or {}
    safety = payload.get("safety") or {}

    lines = [
        "Runtime Handoff Execution",
        f"  status:                 {payload.get('status')}",
        f"  mode:                   {payload.get('mode')}",
        f"  preflight_passed:       {payload.get('preflight_passed')}",
        f"  execution_allowed:      {payload.get('execution_allowed')}",
        f"  handoff_id:             {handoff.get('handoff_id')}",
        f"  verifier_report_id:     {handoff.get('verifier_report_id')}",
        f"  confirmation_id:        {handoff.get('confirmation_id')}",
        f"  recommended_kind:       {handoff.get('recommended_command_kind')}",
    ]
    runtime_execution = payload.get("runtime_execution") or {}
    if runtime_execution:
        lines.extend(
            [
                f"  runtime_execution_id:   {runtime_execution.get('runtime_execution_id')}",
                f"  artifact_path:          {runtime_execution.get('artifact_path')}",
                f"  runner_returned:        {runtime_execution.get('runner_returned')}",
                f"  runner_ok:              {runtime_execution.get('runner_ok')}",
                f"  runner_status:          {runtime_execution.get('runner_status')}",
            ]
        )
    if payload.get("reasons"):
        lines.append("  reasons:")
        for reason in payload.get("reasons") or []:
            lines.append(f"    - {reason}")
    lines.append("  safety:")
    for key in sorted(safety.keys()):
        lines.append(f"    {key}: {safety[key]}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
