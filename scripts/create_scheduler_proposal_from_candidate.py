#!/usr/bin/env python3
"""Create or preview a scheduler proposal from one live scheduler candidate.

Dry-run is the default. This command rediscovers the candidate from the
current task mirror at command time before it delegates to the existing
scheduler proposal generator.

It does not create confirmations, verifier reports, handoffs, runtime audit
events, executor runs, validator results, approvals, merges, cleanup evidence,
GitHub mutations, scheduler loops, or background workers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.scheduler_candidate_proposals import (  # noqa: E402
    SchedulerCandidateProposalRequest,
    candidate_proposal_safety,
    create_scheduler_proposal_from_candidate,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly preview or create a scheduler proposal from one live "
            "scheduler candidate. Dry-run by default; no runtime execution."
        ),
    )
    parser.add_argument(
        "--db-path",
        help=(
            "Path to the Agent Taskflow SQLite state DB. Defaults to "
            "~/.agent-taskflow/state.db."
        ),
    )
    parser.add_argument("--task-key", required=True, help="Candidate task key.")
    parser.add_argument(
        "--artifact-root",
        required=True,
        help="Absolute path under which scheduler_proposals/<id>/ may be written.",
    )
    parser.add_argument(
        "--expected-recommended-command-kind",
        help="Block if the live candidate recommendation has changed.",
    )
    parser.add_argument(
        "--expected-status",
        help="Block if the live candidate status has changed.",
    )
    parser.add_argument(
        "--include-not-ready",
        action="store_true",
        help=(
            "Allow not-ready candidates to be rediscovered for a clear blocked "
            "result. They still cannot create proposal evidence."
        ),
    )
    parser.add_argument(
        "--include-no-action",
        action="store_true",
        help=(
            "Allow no_action candidates to be rediscovered for a clear blocked "
            "result. They still cannot create proposal evidence."
        ),
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; never write to disk or DB (default).",
    )
    mode.add_argument(
        "--confirm-create-proposal",
        action="store_true",
        help=(
            "Create proposal evidence by writing the scheduler proposal "
            "artifact and scheduler_proposal_created event only."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit compact JSON.")
    output.add_argument("--pretty", action="store_true", help="Emit indented JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dry_run = not args.confirm_create_proposal
    try:
        request = SchedulerCandidateProposalRequest(
            db_path=args.db_path,
            task_key=args.task_key,
            artifact_root=args.artifact_root,
            confirm_create_proposal=args.confirm_create_proposal,
            dry_run=dry_run,
            expected_recommended_command_kind=args.expected_recommended_command_kind,
            expected_status=args.expected_status,
            include_not_ready=args.include_not_ready,
            include_no_action=args.include_no_action,
        )
        payload = create_scheduler_proposal_from_candidate(request)
    except ValueError as exc:
        payload = {
            "ok": False,
            "status": "error",
            "mode": "dry_run" if dry_run else "confirmed",
            "task_key": args.task_key,
            "block_reason": None,
            "error": str(exc),
            "candidate": None,
            "proposal": None,
            "safety": candidate_proposal_safety(
                dry_run=dry_run,
                proposal_created=False,
            ),
        }

    _emit(payload, json_output=args.json, pretty_json=args.pretty)

    if payload.get("ok") and payload.get("status") in {"preview", "created"}:
        return 0
    if payload.get("status") == "blocked":
        return 2
    return 1


def _emit(
    payload: dict[str, object],
    *,
    json_output: bool,
    pretty_json: bool,
) -> None:
    if pretty_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(_format_human(payload))


def _format_human(payload: dict[str, object]) -> str:
    lines = [
        "Scheduler Candidate Proposal",
        f"  status:       {payload.get('status')}",
        f"  mode:         {payload.get('mode')}",
        f"  task_key:     {payload.get('task_key')}",
    ]
    if payload.get("block_reason"):
        lines.append(f"  block_reason: {payload.get('block_reason')}")
    if payload.get("error"):
        lines.append(f"  error:        {payload.get('error')}")

    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        lines.extend(
            [
                "",
                "Candidate:",
                f"  status:       {candidate.get('status')}",
                f"  kind:         {candidate.get('recommended_command_kind')}",
                f"  ready:        {candidate.get('candidate_ready')}",
                f"  gate:         {candidate.get('required_next_gate')}",
                f"  action:       {candidate.get('required_operator_action')}",
            ]
        )

    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        proposal_hash = proposal.get("proposal_hash")
        item_hash = proposal.get("item_hash")
        lines.extend(
            [
                "",
                "Proposal:",
                f"  proposal_id:  {proposal.get('proposal_id')}",
                f"  created:      {proposal.get('created')}",
                f"  kind:         {proposal.get('recommended_command_kind')}",
                f"  item_id:      {proposal.get('proposal_item_id')}",
                f"  item_hash:    {_short_hash(item_hash)}",
                f"  hash:         {_short_hash(proposal_hash)}",
                f"  artifact:     {proposal.get('proposal_artifact_path') or '(dry-run; not written)'}",
            ]
        )

    safety = payload.get("safety")
    if isinstance(safety, dict):
        lines.extend(
            [
                "",
                "Safety:",
                f"  dry_run:                  {safety.get('dry_run')}",
                f"  proposal_created:         {safety.get('proposal_created')}",
                f"  confirmation_created:     {safety.get('confirmation_created')}",
                f"  handoff_created:          {safety.get('handoff_created')}",
                f"  runtime_started:          {safety.get('runtime_started')}",
                f"  approved_task_runner:     {safety.get('approved_task_runner_called')}",
                f"  github_mutated:           {safety.get('github_mutated')}",
                f"  not_execution_permission: {safety.get('not_execution_permission')}",
            ]
        )

    return "\n".join(lines)


def _short_hash(value: object) -> object:
    if isinstance(value, str) and len(value) > 12:
        return f"{value[:12]}..."
    return value


if __name__ == "__main__":
    raise SystemExit(main())
