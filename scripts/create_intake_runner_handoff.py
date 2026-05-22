#!/usr/bin/env python3
"""Produce an intake-to-runner handoff artifact.

Dry-run by default. This command does NOT execute any proposed action.
It does NOT push, create PRs, merge, approve, reject, run cleanup,
mutate task status, contact GitHub, start a worktree, start an executor,
start a validator, or start any background worker.

The handoff artifact is intentionally NOT action evidence and NOT
execution permission. It is the structural bridge between the read-only
scheduler confirmation surface and any future runtime gate; the runner
may not start on the strength of this artifact alone.
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

from agent_taskflow.intake_runner_handoff import (  # noqa: E402
    HANDOFF_SAFETY_FLAGS,
    IntakeRunnerHandoffError,
    IntakeRunnerHandoffRequest,
    create_intake_runner_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Produce an intake-to-runner handoff artifact after a scheduler "
            "confirmation verifier report is valid. Read-only by default; "
            "never executes proposed actions; never starts an executor or "
            "validator; never pushes branches; never creates PRs; never "
            "merges; never starts a background worker."
        ),
    )
    parser.add_argument("--db-path", required=True, help="SQLite state DB path.")
    parser.add_argument(
        "--artifact-root",
        required=True,
        help=(
            "Absolute path under which intake_runner_handoffs/<id>/ may "
            "be written."
        ),
    )
    parser.add_argument(
        "--proposal-item-id",
        required=True,
        help=(
            "Exact proposal_item_id within the scheduler confirmation to "
            "build the handoff for."
        ),
    )

    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--confirmation-id",
        help="Build the handoff from the confirmation with this confirmation_id.",
    )
    selector.add_argument(
        "--confirmation-artifact-path",
        help=(
            "Build the handoff from the scheduler_confirmation.json at this "
            "absolute path."
        ),
    )
    selector.add_argument(
        "--latest",
        action="store_true",
        help="Build the handoff from the most recently recorded confirmation.",
    )

    parser.add_argument(
        "--task-key",
        help="Optional expected task_key the confirmed item must match.",
    )
    parser.add_argument(
        "--expected-command-kind",
        help=(
            "Optional expected recommended_command_kind the confirmed item "
            "must match."
        ),
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=None,
        help=(
            "Optional override that may only TIGHTEN the verifier's default "
            "expiration TTL; never loosens it."
        ),
    )
    parser.add_argument(
        "--confirm-create-handoff",
        action="store_true",
        help=(
            "Persist the handoff JSON to <artifact-root>/"
            "intake_runner_handoffs/<handoff_id>/intake_runner_handoff.json "
            "and record per-task intake_runner_handoff evidence. Does NOT "
            "start an executor, validator, push, PR, merge, cleanup, or "
            "background worker."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit a human-readable summary (default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dry_run = not args.confirm_create_handoff

    try:
        request = IntakeRunnerHandoffRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            proposal_item_id=args.proposal_item_id,
            confirmation_id=args.confirmation_id,
            confirmation_artifact_path=(
                Path(args.confirmation_artifact_path).expanduser()
                if args.confirmation_artifact_path
                else None
            ),
            latest=bool(args.latest),
            expected_command_kind=args.expected_command_kind,
            task_key=args.task_key,
            max_age_minutes=args.max_age_minutes,
            dry_run=dry_run,
            confirm_create_handoff=bool(args.confirm_create_handoff),
        )
        payload = create_intake_runner_handoff(request)
    except (ValueError, IntakeRunnerHandoffError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety": dict(HANDOFF_SAFETY_FLAGS),
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

    return 0 if payload.get("ok") else 1


def _format_pretty(payload: dict[str, Any]) -> str:
    proposal = payload.get("proposal") or {}
    confirmation = payload.get("confirmation") or {}
    runner_contract = payload.get("runner_contract") or {}

    proposal_hash = proposal.get("proposal_hash") or ""
    proposal_hash_short = (
        proposal_hash[:12] + "…"
        if isinstance(proposal_hash, str) and proposal_hash
        else "(missing)"
    )
    item_hash = proposal.get("item_hash") or ""
    item_hash_short = (
        item_hash[:12] + "…"
        if isinstance(item_hash, str) and item_hash
        else "(missing)"
    )

    lines = [
        "Intake-to-Runner Handoff",
        f"  status:                              {payload.get('status')}",
        f"  handoff_id:                          {payload.get('handoff_id')}",
        f"  schema_version:                      {payload.get('schema_version')}",
        f"  mode:                                {payload.get('mode')}",
        f"  created_at:                          {payload.get('created_at')}",
        f"  task_key:                            {payload.get('task_key')}",
        f"  recommended_command_kind:            "
        f"{payload.get('recommended_command_kind')}",
        f"  artifact_path:                       "
        f"{payload.get('artifact_path') or '(dry-run; not written)'}",
        "",
        "Confirmation:",
        f"  confirmation_id:                     "
        f"{confirmation.get('confirmation_id')}",
        f"  confirmation_artifact_path:          "
        f"{confirmation.get('confirmation_artifact_path')}",
        f"  verification_status:                 "
        f"{confirmation.get('verification_status')}",
        f"  verification_passed:                 "
        f"{confirmation.get('verification_passed')}",
        f"  eligible_for_command_specific_confirm: "
        f"{confirmation.get('eligible_for_command_specific_confirm')}",
        "",
        "Proposal binding:",
        f"  proposal_id:                         {proposal.get('proposal_id')}",
        f"  proposal_hash:                       {proposal_hash_short}",
        f"  proposal_item_id:                    "
        f"{proposal.get('proposal_item_id')}",
        f"  item_hash:                           {item_hash_short}",
        "",
        "Runner contract:",
        f"  runner_may_start:                    "
        f"{runner_contract.get('runner_may_start')}",
        f"  execution_allowed:                   "
        f"{runner_contract.get('execution_allowed')}",
        f"  execution_performed:                 "
        f"{runner_contract.get('execution_performed')}",
        f"  executor_started:                    "
        f"{runner_contract.get('executor_started')}",
        f"  validators_started:                  "
        f"{runner_contract.get('validators_started')}",
        f"  action_evidence_created:             "
        f"{runner_contract.get('action_evidence_created')}",
        f"  requires_future_runtime_gate:        "
        f"{runner_contract.get('requires_future_runtime_gate')}",
    ]

    error = payload.get("error")
    if error:
        lines.append("")
        lines.append(f"Error: {error}")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
