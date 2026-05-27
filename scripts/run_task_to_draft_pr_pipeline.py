#!/usr/bin/env python3
"""Run the Level 7D task_key to draft PR pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.task_to_draft_pr_pipeline import (  # noqa: E402
    TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS,
    TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
    TASK_TO_DRAFT_PR_PIPELINE_SOURCE,
    TaskToDraftPRPipelineError,
    TaskToDraftPRPipelineRequest,
    run_task_to_draft_pr_pipeline,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Level 7D task_key to draft PR pipeline for exactly one "
            "task_key. Default mode is dry-run."
        ),
    )
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--recommended-command-kind", default=None)
    parser.add_argument("--proposal-max-items", type=int, default=1)
    parser.add_argument("--operator", default=None)
    parser.add_argument("--operator-note", default=None)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base-branch", default=None)
    parser.add_argument("--resume-existing", action="store_true")
    parser.add_argument(
        "--resume-pr-preparation",
        action="store_true",
        help=(
            "Pass resume_existing=True to PR preparation while keeping "
            "--resume-existing scoped to one-shot runtime evidence."
        ),
    )
    parser.add_argument("--confirm-run-one-shot-pipeline", action="store_true")
    parser.add_argument("--confirm-prepare-pr", action="store_true")
    parser.add_argument("--confirm-github-mutations", action="store_true")
    parser.add_argument("--confirm-branch-push", action="store_true")
    parser.add_argument("--confirm-draft-pr", action="store_true")

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry_run = not any(
        (
            args.confirm_run_one_shot_pipeline,
            args.confirm_prepare_pr,
            args.confirm_github_mutations,
            args.confirm_branch_push,
            args.confirm_draft_pr,
        )
    )

    try:
        request = TaskToDraftPRPipelineRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            task_key=args.task_key,
            dry_run=dry_run,
            confirm_run_one_shot_pipeline=bool(args.confirm_run_one_shot_pipeline),
            resume_existing=bool(args.resume_existing),
            resume_pr_preparation=bool(args.resume_pr_preparation),
            confirm_prepare_pr=bool(args.confirm_prepare_pr),
            confirm_github_mutations=bool(args.confirm_github_mutations),
            confirm_branch_push=bool(args.confirm_branch_push),
            confirm_draft_pr=bool(args.confirm_draft_pr),
            operator=args.operator,
            operator_note=args.operator_note,
            proposal_max_items=int(args.proposal_max_items),
            recommended_command_kind=args.recommended_command_kind,
            remote=args.remote,
            base_branch=args.base_branch,
            draft=True,
        )
        payload = run_task_to_draft_pr_pipeline(request)
    except (ValueError, TaskToDraftPRPipelineError) as exc:
        payload = {
            "ok": False,
            "schema_version": TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
            "source": TASK_TO_DRAFT_PR_PIPELINE_SOURCE,
            "status": "failed",
            "mode": "dry_run" if dry_run else "confirmed",
            "failed_stage": "one_shot",
            "task_key": args.task_key,
            "reasons": [str(exc)],
            "stage_result": None,
            "safety": {
                **TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS,
                "dry_run": dry_run,
            },
        }
        _emit_json(payload, compact=args.json and not args.pretty)
        return 1

    _emit_json(payload, compact=args.json and not args.pretty)
    return 0 if payload.get("ok") else 1


def _emit_json(payload: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
