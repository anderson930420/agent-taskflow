#!/usr/bin/env python3
"""Read-only review of scheduler proposal artifacts.

Lists or inspects ``scheduler_proposal`` artifacts recorded by
``create_scheduler_proposal``. Always read-only. This command:

- does NOT confirm or consume a proposal,
- does NOT execute any proposed action,
- does NOT push, create PRs, merge, approve, or cleanup,
- does NOT mutate the SQLite mirror state,
- does NOT contact GitHub or any remote service.

Review output is never action evidence. It must not be interpreted as a
confirmation artifact or as proof that any proposed action ran.
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

from agent_taskflow.scheduler_proposal_review import (  # noqa: E402
    DEFAULT_LIST_LIMIT,
    REVIEW_SAFETY_FLAGS,
    SchedulerProposalReviewError,
    SchedulerProposalReviewRequest,
    list_scheduler_proposals,
    review_scheduler_proposal,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect scheduler proposal artifacts. Read-only: never executes "
            "proposed actions, never mutates state, never contacts GitHub."
        ),
    )
    parser.add_argument("--db-path", required=True, help="SQLite state DB path.")
    parser.add_argument(
        "--artifact-root",
        help="Optional absolute artifact root, included in the review payload.",
    )

    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--list",
        action="store_true",
        help="List recorded scheduler proposal artifacts (summary only).",
    )
    selector.add_argument(
        "--latest",
        action="store_true",
        help="Review the most recently recorded scheduler proposal.",
    )
    selector.add_argument(
        "--proposal-id",
        help="Review the proposal with this proposal_id.",
    )
    selector.add_argument(
        "--artifact-path",
        help="Review the scheduler_proposal.json at this absolute path.",
    )

    parser.add_argument(
        "--list-limit",
        type=int,
        default=DEFAULT_LIST_LIMIT,
        help=(
            f"Maximum proposals returned by --list (default: {DEFAULT_LIST_LIMIT})."
        ),
    )
    parser.add_argument(
        "--no-items",
        action="store_true",
        help="Omit per-item detail (single review only).",
    )
    parser.add_argument(
        "--no-verify-hashes",
        action="store_true",
        help="Skip recomputation of proposal_hash and item_hash.",
    )
    parser.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Force recomputation of hashes (default).",
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

    if args.no_verify_hashes and args.verify_hashes:
        print(
            "error: --verify-hashes and --no-verify-hashes are mutually exclusive",
            file=sys.stderr,
        )
        return 1
    verify_hashes = not args.no_verify_hashes

    artifact_root = (
        Path(args.artifact_root).expanduser() if args.artifact_root else None
    )

    try:
        request = SchedulerProposalReviewRequest(
            db_path=Path(args.db_path).expanduser(),
            artifact_root=artifact_root,
            proposal_id=args.proposal_id,
            artifact_path=(
                Path(args.artifact_path).expanduser() if args.artifact_path else None
            ),
            latest=bool(args.latest),
            include_items=not args.no_items,
            verify_hashes=verify_hashes,
            list_limit=args.list_limit,
        )
    except ValueError as exc:
        _emit_error(args, str(exc))
        return 1

    try:
        if args.list:
            payload = list_scheduler_proposals(request)
            mode = "list"
        elif args.latest or args.proposal_id or args.artifact_path:
            payload = review_scheduler_proposal(request)
            mode = "single"
        else:
            # No selector → default to listing (matches the spec's behavior
            # of returning summaries when nothing specific is requested).
            payload = list_scheduler_proposals(request)
            mode = "list"
    except SchedulerProposalReviewError as exc:
        _emit_error(args, str(exc))
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_pretty(payload, mode))
    return 0 if payload.get("ok") else 2


def _emit_error(args: argparse.Namespace, message: str) -> None:
    error_payload = {
        "ok": False,
        "review_status": "error",
        "error": message,
        "safety": dict(REVIEW_SAFETY_FLAGS),
    }
    if args.json:
        print(json.dumps(error_payload, indent=2, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)


def _format_pretty(payload: dict[str, Any], mode: str) -> str:
    if mode == "list":
        return _format_list_pretty(payload)
    return _format_single_pretty(payload)


def _format_list_pretty(payload: dict[str, Any]) -> str:
    proposals = payload.get("proposals") or []
    lines = [
        "Scheduler Proposal Review (list)",
        f"  ok:                 {payload.get('ok')}",
        f"  proposal_count:     {payload.get('proposal_count')}",
        f"  total_recorded:     {payload.get('total_recorded')}",
        f"  list_limit:         {payload.get('list_limit')}",
        f"  schema_version:     {payload.get('schema_version')}",
        f"  read_only_review:   true",
        "",
    ]
    if not proposals:
        lines.append("Proposals: (none)")
        return "\n".join(lines)

    lines.append("Proposals:")
    for proposal in proposals:
        proposal_hash = proposal.get("proposal_hash") or ""
        hash_short = proposal_hash[:12] + "…" if proposal_hash else "(missing)"
        lines.append("")
        lines.append(
            f"  {proposal.get('proposal_id')} "
            f"[{proposal.get('review_status')}]"
        )
        lines.append(f"    proposal_hash:   {hash_short}")
        lines.append(f"    schema_version:  {proposal.get('schema_version')}")
        lines.append(f"    mode:            {proposal.get('mode')}")
        lines.append(f"    created_at:      {proposal.get('created_at')}")
        lines.append(f"    item_count:      {proposal.get('item_count')}")
        lines.append(f"    task_key_count:  {proposal.get('task_key_count')}")
        lines.append(f"    artifact_path:   {proposal.get('artifact_path')}")
        if proposal.get("on_disk_error"):
            lines.append(f"    on_disk_error:   {proposal['on_disk_error']}")
    return "\n".join(lines)


def _format_single_pretty(payload: dict[str, Any]) -> str:
    proposal_hash = payload.get("proposal_hash") or ""
    hash_short = proposal_hash[:12] + "…" if proposal_hash else "(missing)"
    summary = payload.get("proposal_summary") or {}
    selector = payload.get("selector") or {}
    lines = [
        "Scheduler Proposal Review",
        f"  ok:                 {payload.get('ok')}",
        f"  review_status:      {payload.get('review_status')}",
        f"  proposal_id:        {payload.get('proposal_id')}",
        f"  proposal_hash:      {hash_short}",
        f"  hash_valid:         {payload.get('hash_valid')}",
        f"  verify_hashes:      {payload.get('verify_hashes')}",
        f"  schema_version:     {payload.get('schema_version_on_disk')}",
        f"  mode:               {payload.get('mode')}",
        f"  created_at:         {payload.get('created_at')}",
        f"  selector:           {selector.get('kind')}={selector.get('value')}",
        f"  artifact_path:      {payload.get('artifact_path')}",
        f"  read_only_review:   true",
        f"  item_count:         {summary.get('item_count')}",
        f"  executable_count:   {summary.get('executable_count')}",
        f"  warning_count:      {summary.get('warning_count')}",
    ]
    if payload.get("error"):
        lines.append(f"  error:              {payload['error']}")
    db_keys = payload.get("db_task_keys") or []
    if db_keys:
        lines.append(f"  db_task_keys:       {', '.join(db_keys)}")

    items = payload.get("items")
    if isinstance(items, list) and items:
        lines.append("")
        lines.append("Items:")
        for item in items:
            item_hash = item.get("item_hash") or ""
            item_hash_short = item_hash[:12] + "…" if item_hash else "(missing)"
            valid_marker = ""
            if item.get("item_hash_valid") is True:
                valid_marker = " hash-valid"
            elif item.get("item_hash_valid") is False:
                valid_marker = " HASH-INVALID"
            executable = "executable" if item.get("executable") else "needs-inspection"
            lines.append("")
            lines.append(
                f"  {item.get('proposal_item_id')} "
                f"[{item.get('status')} → {item.get('expected_status')}]"
                f" ({executable}){valid_marker}"
            )
            lines.append(f"    task_key:           {item.get('task_key')}")
            lines.append(f"    command_kind:       {item.get('recommended_command_kind')}")
            lines.append(f"    item_hash:          {item_hash_short}")
            lines.append(f"    expected_phase:     {item.get('expected_phase_label')}")
            lines.append(
                f"    severity/conf:      {item.get('severity')} / {item.get('confidence')}"
            )
            lines.append(f"    proposed_action:    {item.get('proposed_action')}")
            lines.append(f"    reason:             {item.get('reason')}")
            warnings = item.get("consistency_warnings") or []
            if warnings:
                lines.append("    consistency_warnings:")
                for warning in warnings:
                    lines.append(f"      - {warning}")
    elif items is None:
        lines.append("")
        lines.append("Items: (omitted via --no-items)")
    else:
        lines.append("")
        lines.append("Items: (none)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
