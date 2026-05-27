#!/usr/bin/env python3
"""Run the Level 8B one-task-at-a-time confirmed scheduler watcher."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.scheduler_watcher_one_task import (  # noqa: E402
    SchedulerWatcherOneTaskError,
    SchedulerWatcherOneTaskRequest,
    WATCHER_ONE_TASK_SAFETY_FLAGS,
    WATCHER_ONE_TASK_SCHEMA_VERSION,
    WATCHER_ONE_TASK_SOURCE,
    run_scheduler_watcher_one_task,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Level 8B one-task-at-a-time confirmed scheduler watcher. "
            "Default mode is dry-run. The confirmed mode runs exactly one "
            "selected candidate through the task-to-draft-PR pipeline and stops."
        )
    )
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--artifact-root", required=True)

    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--recommended-command-kind", default=None)

    parser.add_argument("--task-key", default=None)
    parser.add_argument("--select-first-candidate", action="store_true")
    parser.add_argument("--confirm-select-first-candidate", action="store_true")

    parser.add_argument("--confirm-run-watcher-one-task", action="store_true")
    parser.add_argument("--confirm-run-one-shot-pipeline", action="store_true")
    parser.add_argument("--confirm-prepare-pr", action="store_true")
    parser.add_argument("--confirm-github-mutations", action="store_true")
    parser.add_argument("--confirm-branch-push", action="store_true")
    parser.add_argument("--confirm-draft-pr", action="store_true")

    parser.add_argument("--resume-existing", action="store_true")
    parser.add_argument("--resume-pr-preparation", action="store_true")

    parser.add_argument("--operator", default=None)
    parser.add_argument("--operator-note", default=None)
    parser.add_argument("--proposal-max-items", type=int, default=1)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base-branch", default=None)

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry_run = not bool(args.confirm_run_watcher_one_task)

    try:
        request = SchedulerWatcherOneTaskRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            dry_run=dry_run,
            confirm_run_watcher_one_task=bool(args.confirm_run_watcher_one_task),
            limit=int(args.limit),
            project=args.project,
            status=args.status,
            recommended_command_kind=args.recommended_command_kind,
            task_key=args.task_key,
            select_first_candidate=bool(args.select_first_candidate),
            confirm_select_first_candidate=bool(
                args.confirm_select_first_candidate
            ),
            resume_existing=bool(args.resume_existing),
            resume_pr_preparation=bool(args.resume_pr_preparation),
            confirm_run_one_shot_pipeline=bool(args.confirm_run_one_shot_pipeline),
            confirm_prepare_pr=bool(args.confirm_prepare_pr),
            confirm_github_mutations=bool(args.confirm_github_mutations),
            confirm_branch_push=bool(args.confirm_branch_push),
            confirm_draft_pr=bool(args.confirm_draft_pr),
            operator=args.operator,
            operator_note=args.operator_note,
            proposal_max_items=int(args.proposal_max_items),
            remote=args.remote,
            base_branch=args.base_branch,
            draft=True,
        )
        payload = run_scheduler_watcher_one_task(request)
    except (ValueError, SchedulerWatcherOneTaskError) as exc:
        payload = {
            "ok": False,
            "schema_version": WATCHER_ONE_TASK_SCHEMA_VERSION,
            "source": WATCHER_ONE_TASK_SOURCE,
            "status": "error",
            "mode": "dry_run" if dry_run else "confirmed",
            "failed_stage": "preview",
            "reasons": [str(exc)],
            "preview": None,
            "selected_candidate": None,
            "stage_result": None,
            "safety": dict(WATCHER_ONE_TASK_SAFETY_FLAGS),
        }
        _emit(payload, pretty=bool(args.pretty), compact=bool(args.json))
        return 1

    _emit(payload, pretty=bool(args.pretty), compact=bool(args.json))
    return 0 if payload.get("ok") else 1


def _emit(payload: dict[str, object], *, pretty: bool, compact: bool) -> None:
    if compact and not pretty:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
