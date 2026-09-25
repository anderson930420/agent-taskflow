#!/usr/bin/env python3
"""Run one integration-tick pass for one repository and exit (SPEC §47.2).

One bounded pass, in a fixed order and once each: PR-outcome polling,
verified-merge cleanup, target-freshness polling, then the FIFO drain of the
repository's initial queue snapshot. Each phase is a read-only preview unless
its own confirmation flag is passed: --confirm-pr-poll, --confirm-cleanup,
--confirm-freshness and --confirm-integration (the drain). With none of them
the tick writes nothing to the database, pushes nothing and changes no PR.
Cancelled-route cleanup and remote-branch deletion are never performed.

The run holds a non-overlap lock keyed by (database, repository) (§47.3). A
second invocation for the same pair prints one skipped_overlap JSON result and
exits immediately, doing no work. Exit codes: 0 ok, 1 not ok (a phase or
drain outcome needs attention), 2 error, 75 skipped_overlap.

The integration validators are the execution policy's (RULINGS 67): the one
project registered in config/projects.yaml for --repo at --repo-path must have
a valid execution: block, whose integration_validators the drain runs. A
repository without one is refused (exit 2) before anything runs. The former
--validator-config JSON, a second validator list kept by cron, is refused too.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.execution_policy import (
    ExecutionPolicyError,
    resolve_repository_execution_policy,
)
from agent_taskflow.integration_tick import IntegrationTickRequest, run_integration_tick
from agent_taskflow.tick_lock import (
    EXIT_SKIPPED_OVERLAP,
    TickLock,
    integration_tick_lock_path,
    skipped_overlap_result,
)

KIND = "integration_tick"
VALIDATOR_CONFIG_REFUSED = "validator_config_refused"


def _emit(value: dict[str, Any], *, jsonl: bool) -> None:
    if jsonl:
        print(json.dumps(value, sort_keys=True))
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--repo", required=True, help="Exact GitHub owner/name key stored in the queue")
    parser.add_argument("--repo-path", required=True, type=Path)
    # Refused, not merely ignored, so a stale cron line fails loudly.
    parser.add_argument("--validator-config", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--target-branch", default="main")
    parser.add_argument("--remote", default="origin")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--confirm-integration", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="Read-only preview (the default)")
    parser.add_argument("--confirm-pr-poll", action="store_true",
                        help="Let the PR-outcome poll record §32.1 fields and apply §33/§35 outcomes")
    parser.add_argument("--confirm-cleanup", action="store_true",
                        help="Let verified-merge cleanup remove the worktree and local branch")
    parser.add_argument("--confirm-freshness", action="store_true",
                        help="Let the target-freshness poll re-queue stale needs_review Tickets")
    parser.add_argument("--jsonl", action="store_true",
                        help="Print the result as one compact JSON line, for append-only logs")
    args = parser.parse_args(argv)
    if args.dry_run and (args.confirm_pr_poll or args.confirm_cleanup or args.confirm_freshness):
        parser.error("--dry-run cannot be combined with a --confirm-* flag")
    if args.validator_config is not None:
        print(json.dumps({
            "kind": KIND, "ok": False, "status": "error", "reason_code": VALIDATOR_CONFIG_REFUSED,
            "reason": (
                "--validator-config is no longer accepted: integration validators come only "
                "from the project's execution: policy in config/projects.yaml (RULINGS 67)"
            ),
        }, sort_keys=True))
        return 2
    try:
        policy = resolve_repository_execution_policy(
            github_repo=args.repo.strip(), repo_path=args.repo_path,
        )
    except ExecutionPolicyError as exc:
        print(json.dumps({"kind": KIND, "ok": False, "status": "error",
                          "reason_code": exc.reason_code, "reason": str(exc)}, sort_keys=True))
        return 2
    try:
        request = IntegrationTickRequest(
            repo=args.repo, repo_path=args.repo_path, db_path=args.db_path,
            validator_specs=policy.integration_validator_specs(),
            target_branch=args.target_branch, remote=args.remote,
            dry_run=not args.confirm_integration,
            confirm_integration=args.confirm_integration,
            consumer_phases=True,
            confirm_pr_poll=args.confirm_pr_poll,
            confirm_cleanup=args.confirm_cleanup,
            confirm_freshness=args.confirm_freshness,
        )
        # Never leave a lock file beside a database that does not exist.
        if not request.db_path.is_file():
            raise ValueError(f"db_path must name an existing initialized database: {request.db_path}")
        # RULINGS 70 (F10-FU4): the lock is always the derived path; there is
        # no --lock-path override.
        lock = TickLock(
            integration_tick_lock_path(request.db_path, request.repo),
            holder={"kind": KIND, "repo": request.repo, "db_path": str(request.db_path)},
            db_path=request.db_path,
        )
        acquired = lock.acquire()
    except Exception as exc:
        print(json.dumps({"kind": KIND, "ok": False, "status": "error",
                          "reason": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    if not acquired:
        _emit(skipped_overlap_result(KIND, lock, repo=request.repo, db_path=str(request.db_path)),
              jsonl=args.jsonl)
        return EXIT_SKIPPED_OVERLAP
    # A corrupt holder record that the lock reclaimed is reported on this run's line.
    reclaim = {"lock_reclaimed": lock.reclaimed} if lock.reclaimed else {}
    try:
        result = run_integration_tick(request)
    except Exception as exc:
        print(json.dumps({"kind": KIND, "ok": False, "status": "error",
                          "reason": f"{type(exc).__name__}: {exc}", **reclaim}, sort_keys=True))
        return 2
    finally:
        lock.release()
    result["execution_policy"] = {
        "project": policy.project,
        "policy_version": policy.policy_version,
        "policy_sha256": policy.sha256,
    }
    _emit({**result, **reclaim}, jsonl=args.jsonl)
    if result.get("tick_status") == "error":
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
