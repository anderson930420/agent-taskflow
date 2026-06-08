#!/usr/bin/env python3
"""Read-only CLI that normalizes one execution payload into a unified summary.

This is the P4-e observability CLI. It reads exactly one JSON object (from a file
or stdin), normalizes it with the selected summarizer, and emits a
:class:`UnifiedExecutionSummary`.

It is strictly read-only normalization. It does not read the DB, tail logs, call
git or GitHub, run an executor or validator, or mutate the filesystem. It exposes
no merge, approval, cleanup, archive, closeout, publication, push, branch- or
worktree-deletion behavior. Human review remains the final gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.execution_observability import (  # noqa: E402
    SUMMARY_SOURCE_APPROVED_TASK_RUNNER,
    SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
    SUMMARY_SOURCE_SCHEDULER_TICK,
    UnifiedExecutionSummary,
    summarize_approved_task_runner_payload,
    summarize_execution_engine_result,
    summarize_scheduler_tick_payload,
    to_observability_dict,
)


SAFETY_EPILOG = (
    "Safety:\n"
    "  This is read-only normalization only. It does not read the DB, tail logs,\n"
    "  call git or GitHub, or run an executor or validator. It does not approve,\n"
    "  merge, clean up, archive, close out, publish a PR, push, or delete a branch\n"
    "  or worktree. Human review remains the final gate."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize one execution payload (ExecutionEngineResult, "
            "approved_task_runner, or scheduler tick JSON) into a unified "
            "execution observability summary. Read-only."
        ),
        epilog=SAFETY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=[
            SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
            SUMMARY_SOURCE_APPROVED_TASK_RUNNER,
            SUMMARY_SOURCE_SCHEDULER_TICK,
        ],
        default=SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
        help=(
            "Which summarizer to apply. Default: manual_engine_facade "
            "(ExecutionEngineResult shape)."
        ),
    )
    parser.add_argument(
        "--input",
        help="Path to a JSON payload file. When omitted, JSON is read from stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Emit JSON output. This is the default.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (implies JSON).",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Emit a concise human-readable summary instead of JSON.",
    )
    return parser


def _load_payload(args: argparse.Namespace) -> object:
    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    return json.loads(raw)


def _summarize(source: str, payload: object) -> UnifiedExecutionSummary:
    if source == SUMMARY_SOURCE_SCHEDULER_TICK:
        return summarize_scheduler_tick_payload(payload)
    if source == SUMMARY_SOURCE_APPROVED_TASK_RUNNER:
        return summarize_approved_task_runner_payload(payload)
    return summarize_execution_engine_result(
        payload, source=SUMMARY_SOURCE_MANUAL_ENGINE_FACADE
    )


def _emit(summary: UnifiedExecutionSummary, args: argparse.Namespace) -> None:
    if args.text:
        _emit_text(summary)
        return
    payload = to_observability_dict(summary)
    if args.pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, sort_keys=True))


def _emit_text(summary: UnifiedExecutionSummary) -> None:
    print(f"source: {summary.source}")
    print(f"task_key: {summary.task_key}")
    print(f"status: {summary.status}")
    print(f"ok: {summary.ok}")
    if summary.dry_run is not None:
        print(f"dry_run: {summary.dry_run}")
    next_action = summary.next_operator_action or "human review (no automated next action)"
    print(f"next action: {next_action}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        payload = _load_payload(args)
    except FileNotFoundError as exc:
        print(f"error: input file not found: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    summary = _summarize(args.source, payload)
    _emit(summary, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
