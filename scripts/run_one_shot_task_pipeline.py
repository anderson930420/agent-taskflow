#!/usr/bin/env python3
"""Run the Level 7A task_key based one-shot pipeline.

Dry-run by default. With --confirm-run-one-shot-pipeline the command
walks the existing operator-gated chain (scheduler_proposal ->
scheduler_confirmation -> scheduler_confirmation_verifier_report ->
intake_runner_handoff -> runtime preflight -> approved_task_runner ->
runtime_handoff_execution) for exactly one task_key. It does not start
a scheduler loop, a background worker, or automatic task picking, and
does not approve, merge, clean up, push, or create PRs.
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

from agent_taskflow.one_shot_task_pipeline import (  # noqa: E402
    ONE_SHOT_PIPELINE_SAFETY_FLAGS,
    OneShotTaskPipelineError,
    OneShotTaskPipelineRequest,
    run_one_shot_task_pipeline,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Level 7A task_key based one-shot pipeline. "
            "Dry-run by default. With --confirm-run-one-shot-pipeline "
            "the chain runs end-to-end for exactly one task_key."
        ),
    )
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--recommended-command-kind", default=None)
    parser.add_argument("--proposal-max-items", type=int, default=1)
    parser.add_argument("--operator", default=None)
    parser.add_argument("--operator-note", default=None)
    parser.add_argument(
        "--confirm-run-one-shot-pipeline",
        action="store_true",
        help=(
            "Confirm the one-shot pipeline. Without this flag the command "
            "runs in dry-run, writes nothing, and never calls "
            "approved_task_runner."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry_run = not args.confirm_run_one_shot_pipeline

    try:
        request = OneShotTaskPipelineRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            task_key=args.task_key,
            dry_run=dry_run,
            confirm_run_one_shot_pipeline=bool(
                args.confirm_run_one_shot_pipeline
            ),
            operator=args.operator,
            operator_note=args.operator_note,
            proposal_max_items=int(args.proposal_max_items),
            recommended_command_kind=args.recommended_command_kind,
        )
        payload = run_one_shot_task_pipeline(request)
    except (ValueError, OneShotTaskPipelineError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(ONE_SHOT_PIPELINE_SAFETY_FLAGS),
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
    safety = payload.get("safety") or {}
    stages = payload.get("stages") or {}
    lines = [
        "One-Shot Task Pipeline",
        f"  status:           {payload.get('status')}",
        f"  mode:             {payload.get('mode')}",
        f"  task_key:         {payload.get('task_key')}",
        f"  final_task_status:{payload.get('final_task_status')}",
        f"  failed_stage:     {payload.get('failed_stage')}",
    ]
    for stage_name in (
        "proposal",
        "confirmation",
        "verifier_report",
        "handoff",
        "runtime_execution",
    ):
        info = stages.get(stage_name) or {}
        lines.append(f"  {stage_name}:")
        for key in sorted(info.keys()):
            lines.append(f"    {key}: {info[key]}")
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
