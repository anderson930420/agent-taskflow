#!/usr/bin/env python3
"""Manual, opt-in ExecutionEngine facade runner for one approved task (P4-d).

This is the single explicit, opt-in runtime path that runs an approved task
through the ExecutionEngine facade:

    CLI
        -> build_manual_execution_engine_request(...)
        -> ApprovedTaskRunnerExecutionEngineAdapter
        -> approved_task_runner.run_approved_task
        -> ExecutionEngineResult

It does not change the scheduler tick, one-task automation, dispatcher, or cron.
Dry-run is the default. Non-dry-run requires --confirm-execution-engine-run.
Confirmation only allows invoking the existing approved task runner through the
adapter. It does not approve, merge, clean up, archive, close out, publish a PR,
delete a branch or worktree, close an issue, or mutate GitHub. Human review
remains the final gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.execution_engine_contract import (  # noqa: E402
    EXECUTION_STATUS_BLOCKED,
    REQUEST_SOURCE_MANUAL,
    ExecutionEngineResult,
    ExecutionEngineSafety,
    to_json_dict,
)
from agent_taskflow.execution_engine_manual_runtime import (  # noqa: E402
    build_manual_execution_engine_request,
    run_manual_execution_engine_request,
)


SAFETY_EPILOG = (
    "Safety:\n"
    "  Dry-run is the default. Non-dry-run requires --confirm-execution-engine-run.\n"
    "  Confirmation only allows invoking the existing approved task runner through\n"
    "  the ExecutionEngine adapter. It does not approve, merge, clean up, archive,\n"
    "  close out, publish a PR, delete a branch or worktree, close an issue, or\n"
    "  mutate GitHub. Human review remains the final gate."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one approved task through the ExecutionEngine facade. This is "
            "an explicit, opt-in manual runtime path. Dry-run by default."
        ),
        epilog=SAFETY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Explicit task key to run, for example AT-GH-123.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the repository root for the selected task.",
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        help="Absolute path to the artifact directory for the selected task.",
    )
    parser.add_argument(
        "--executor",
        default="noop",
        help="Explicit executor name. Default: noop.",
    )
    parser.add_argument(
        "--validator",
        action="append",
        dest="validators",
        help=(
            "Validator name to run through the facade. May be repeated. When "
            "omitted, the approved task runner's own validator defaults apply."
        ),
    )
    dry_run_group = parser.add_mutually_exclusive_group()
    dry_run_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview the request without dispatching. This is the default.",
    )
    dry_run_group.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Disable dry-run. Requires --confirm-execution-engine-run.",
    )
    parser.set_defaults(dry_run=True)
    parser.add_argument(
        "--confirm-execution-engine-run",
        dest="confirm_execution_engine_run",
        action="store_true",
        help=(
            "Required for non-dry-run. Only permits invoking the existing "
            "approved task runner through the adapter; implies no approval, "
            "merge, cleanup, publication, or GitHub mutation."
        ),
    )
    preflight_group = parser.add_mutually_exclusive_group()
    preflight_group.add_argument(
        "--preflight",
        dest="preflight",
        action="store_true",
        help="Run executor preflight before execution. This is the default.",
    )
    preflight_group.add_argument(
        "--no-preflight",
        dest="preflight",
        action="store_false",
        help="Skip executor preflight.",
    )
    parser.set_defaults(preflight=True)
    parser.add_argument(
        "--model",
        help="Executor model override, for example claude.",
    )
    parser.add_argument(
        "--provider",
        help="Executor provider override, for example anthropic.",
    )
    parser.add_argument(
        "--tools",
        action="append",
        dest="tools",
        help="Executor tool to enable. May be repeated.",
    )
    parser.add_argument(
        "--pi-bin",
        help="Absolute path to the pi binary when --executor pi is selected.",
    )
    parser.add_argument(
        "--worktree-root",
        help="Root directory for isolated task worktrees.",
    )
    parser.add_argument(
        "--runtime-handoff-path",
        help="Optional path to a runtime handoff artifact for context only.",
    )
    parser.add_argument(
        "--verifier-report-path",
        help="Optional path to a verifier report artifact for context only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text summary.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (implies JSON).",
    )
    return parser


def _blocked_result(
    task_key: str,
    *,
    summary: str,
    error: str,
    next_action: str,
) -> ExecutionEngineResult:
    """Build a conservative blocked ExecutionEngineResult for the CLI."""

    return ExecutionEngineResult(
        ok=False,
        task_key=task_key,
        status=EXECUTION_STATUS_BLOCKED,
        summary=summary,
        next_operator_action=next_action,
        safety=ExecutionEngineSafety(),
        metadata={
            "source": REQUEST_SOURCE_MANUAL,
            "path": "execution_engine_facade",
            "error": error,
            "warning": error,
        },
    )


def _emit(result: ExecutionEngineResult, args: argparse.Namespace) -> None:
    if args.json or args.pretty:
        payload = to_json_dict(result)
        if args.json and not args.pretty:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return
    _emit_text(result)


def _emit_text(result: ExecutionEngineResult) -> None:
    print(f"status: {result.status}")
    print(f"task key: {result.task_key}")
    print(f"ok: {result.ok}")
    if result.summary:
        print(f"summary: {result.summary}")
    next_action = result.next_operator_action or "human review (no automated next action)"
    print(f"next action: {next_action}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    task_key = (args.task_key or "").strip() or "UNKNOWN"

    if not args.dry_run and not args.confirm_execution_engine_run:
        message = (
            "Refusing non-dry-run execution without --confirm-execution-engine-run. "
            "Re-run with --confirm-execution-engine-run to execute the approved task "
            "runner through the ExecutionEngine adapter, or omit --no-dry-run to preview."
        )
        result = _blocked_result(
            task_key,
            summary=message,
            error="missing --confirm-execution-engine-run",
            next_action=(
                "Re-run with --confirm-execution-engine-run for non-dry-run execution."
            ),
        )
        _emit(result, args)
        return 1

    try:
        request = build_manual_execution_engine_request(
            task_key=args.task_key,
            repo_path=args.repo_path,
            artifact_dir=args.artifact_dir,
            executor=args.executor,
            validators=tuple(args.validators or ()),
            dry_run=args.dry_run,
            preflight=args.preflight,
            model=args.model,
            provider=args.provider,
            tools=tuple(args.tools or ()),
            pi_bin=args.pi_bin,
            worktree_root=args.worktree_root,
            runtime_handoff_path=args.runtime_handoff_path,
            verifier_report_path=args.verifier_report_path,
        )
    except ValueError as exc:
        result = _blocked_result(
            task_key,
            summary=f"Invalid manual ExecutionEngine request: {exc}",
            error=str(exc),
            next_action="Fix the invalid input and re-run.",
        )
        _emit(result, args)
        return 1

    result = run_manual_execution_engine_request(request)
    _emit(result, args)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
