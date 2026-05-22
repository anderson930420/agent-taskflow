#!/usr/bin/env python3
"""Dry-run-only verification of one scheduler confirmation item.

Read-only. Answers a single yes/no question:

    "Would this exact scheduler_confirmation item be valid to attempt
     consumption now?"

This command does NOT execute any proposed action. It does NOT push,
create PRs, merge, approve, reject, run cleanup, mutate task status,
contact GitHub, or start any background worker. It does NOT consume the
confirmation, does NOT write consumption evidence, and does NOT mutate
the SQLite mirror.

The verifier output is itself a dry-run report. It is never action
evidence and must never be interpreted as such by downstream readers.
Existing command-specific ``--confirm-*`` helpers remain the only
mutation gates.
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

from agent_taskflow.scheduler_confirmation_verifier import (  # noqa: E402
    SchedulerConfirmationVerificationRequest,
    SchedulerConfirmationVerifierError,
    STATUS_VALID,
    VERIFIER_SAFETY_FLAGS,
    verify_scheduler_confirmation_item,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify whether one scheduler_confirmation item is currently "
            "valid to attempt consumption. Read-only: never executes the "
            "proposed action, never mutates state, never contacts GitHub. "
            "Does not bypass command-specific --confirm-* helpers."
        ),
    )
    parser.add_argument("--db-path", required=True, help="SQLite state DB path.")
    parser.add_argument(
        "--artifact-root",
        help="Optional absolute artifact root, included in the verification payload.",
    )

    parser.add_argument(
        "--confirmation-id",
        help="Verify the confirmation with this confirmation_id.",
    )
    parser.add_argument(
        "--confirmation-artifact-path",
        help="Verify the scheduler_confirmation.json at this absolute path.",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Verify against the most recently recorded scheduler_confirmation.",
    )

    parser.add_argument(
        "--proposal-item-id",
        required=True,
        help="Exact proposal_item_id within the confirmation to verify.",
    )
    parser.add_argument(
        "--expected-command-kind",
        help=(
            "Optional command kind the item must match (e.g. "
            "branch_push_review). Blocking on mismatch."
        ),
    )
    parser.add_argument(
        "--task-key",
        help=(
            "Optional task_key the item must match. Blocking on mismatch."
        ),
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        help=(
            "Override default expiration (minutes) for this verification. "
            "Useful for tests/smoke determinism; never bypasses expiration."
        ),
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit a human-readable summary (default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    selectors = sum(
        1
        for selector in (
            args.confirmation_id,
            args.confirmation_artifact_path,
            args.latest,
        )
        if selector
    )
    if selectors == 0:
        _emit_error(
            args,
            "verification requires one of --confirmation-id, "
            "--confirmation-artifact-path, or --latest",
        )
        return 1
    if selectors > 1:
        _emit_error(
            args,
            "verification accepts only one of --confirmation-id, "
            "--confirmation-artifact-path, --latest",
        )
        return 1

    artifact_root = (
        Path(args.artifact_root).expanduser() if args.artifact_root else None
    )
    confirmation_artifact_path = (
        Path(args.confirmation_artifact_path).expanduser()
        if args.confirmation_artifact_path
        else None
    )

    try:
        request = SchedulerConfirmationVerificationRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=artifact_root,
            confirmation_id=args.confirmation_id,
            confirmation_artifact_path=confirmation_artifact_path,
            latest=bool(args.latest),
            proposal_item_id=args.proposal_item_id,
            expected_command_kind=args.expected_command_kind,
            task_key=args.task_key,
            max_age_minutes=args.max_age_minutes,
        )
    except ValueError as exc:
        _emit_error(args, str(exc))
        return 1

    try:
        payload = verify_scheduler_confirmation_item(request)
    except SchedulerConfirmationVerifierError as exc:
        _emit_error(args, str(exc))
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_pretty(payload))
    return 0 if payload.get("status") == STATUS_VALID else 2


def _emit_error(args: argparse.Namespace, message: str) -> None:
    error_payload = {
        "ok": False,
        "status": "error",
        "error": message,
        "safety": dict(VERIFIER_SAFETY_FLAGS),
    }
    if args.json:
        print(json.dumps(error_payload, indent=2, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)


def _format_pretty(payload: dict[str, Any]) -> str:
    selector = payload.get("selector") or {}
    expiration = payload.get("expiration") or {}
    revalidation = payload.get("revalidation") or {}
    item_hash = payload.get("item_hash") or ""
    item_hash_short = item_hash[:12] + "…" if item_hash else "(missing)"
    proposal_hash = payload.get("proposal_hash") or ""
    proposal_hash_short = (
        proposal_hash[:12] + "…" if proposal_hash else "(missing)"
    )

    lines = [
        "Scheduler Confirmation Verification",
        f"  ok:                    {payload.get('ok')}",
        f"  status:                {payload.get('status')}",
        f"  verification_passed:   {payload.get('verification_passed')}",
        f"  eligible_for_command_specific_confirm: "
        f"{payload.get('eligible_for_command_specific_confirm')}",
        f"  execution_allowed:     {payload.get('execution_allowed')}",
        f"  execution_performed:   {payload.get('execution_performed')}",
        f"  action_evidence_created: {payload.get('action_evidence_created')}",
        f"  schema_version:        {payload.get('schema_version')}",
        f"  source:                {payload.get('source')}",
        f"  selector:              {selector.get('kind')}={selector.get('value')}",
        f"  confirmation_id:       {payload.get('confirmation_id')}",
        f"  confirmation_path:     {payload.get('confirmation_artifact_path')}",
        f"  confirmation_schema:   {payload.get('confirmation_schema_version')}",
        f"  confirmation_created:  {payload.get('confirmation_created_at')}",
        f"  proposal_id:           {payload.get('proposal_id')}",
        f"  proposal_hash:         {proposal_hash_short}",
        f"  proposal_item_id:      {payload.get('proposal_item_id')}",
        f"  item_hash:             {item_hash_short}",
        f"  task_key:              {payload.get('task_key')}",
        f"  command_kind:          {payload.get('recommended_command_kind')}",
    ]
    if expiration:
        lines.append("  expiration:")
        lines.append(f"    max_age_minutes:     {expiration.get('max_age_minutes')}")
        lines.append(f"    max_age_source:      {expiration.get('max_age_source')}")
        lines.append(f"    age_seconds:         {expiration.get('age_seconds')}")
        lines.append(f"    expired:             {expiration.get('expired')}")
        if expiration.get("detail"):
            lines.append(f"    detail:              {expiration['detail']}")
    if revalidation:
        lines.append("  revalidation:")
        lines.append(
            f"    task_exists:                       {revalidation.get('task_exists')}"
        )
        lines.append(
            f"    task_status_matches_expected:      {revalidation.get('task_status_matches_expected')}"
        )
        lines.append(
            f"    current_phase_label_matches:       {revalidation.get('current_phase_label_matches_expected')}"
        )
        lines.append(
            f"    current_recommendation_kind:       {revalidation.get('current_recommendation_kind')}"
        )
        lines.append(
            f"    current_recommendation_kind_matches: {revalidation.get('current_recommendation_kind_matches')}"
        )
        lines.append(
            f"    current_item_hash_recomputed:      {revalidation.get('current_item_hash_recomputed')}"
        )
        lines.append(
            f"    current_item_hash_matches:         {revalidation.get('current_item_hash_matches')}"
        )
        lines.append(
            f"    warnings_acceptable:               {revalidation.get('warnings_acceptable')}"
        )
    checks = payload.get("checks") or []
    if checks:
        lines.append("  checks:")
        for check in checks:
            mark = "ok" if check.get("passed") else "BLOCK"
            lines.append(f"    [{mark}] {check.get('name')}")
            if check.get("detail"):
                lines.append(f"        detail: {check['detail']}")
    safety = payload.get("safety") or {}
    lines.append("  safety:")
    lines.append(f"    dry_run_only:                {safety.get('dry_run_only')}")
    lines.append(f"    will_mutate_db:              {safety.get('will_mutate_db')}")
    lines.append(f"    will_mutate_github:          {safety.get('will_mutate_github')}")
    lines.append(
        f"    will_change_task_status:     {safety.get('will_change_task_status')}"
    )
    lines.append(
        f"    will_start_background_worker:{safety.get('will_start_background_worker')}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
