"""Packaged CLI for the Codex advisory reviewer contract.

Dry-run is the default and invokes no subprocess. ``--confirm-run`` is the
explicit opt-in that invokes the Codex CLI exactly once. In every mode Codex is
advisory only: it is never deterministic validation authority and never
approves, blocks, merges, pushes, cleans up, deletes branches/worktrees, or
changes lifecycle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_taskflow.codex_advisory_review import (
    DEFAULT_TIMEOUT_SECONDS,
    CodexAdvisoryReviewError,
    CodexAdvisoryReviewRequest,
    generate_codex_advisory_review,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Codex advisory review artifacts for an existing task "
            "artifact directory. Dry-run is the default and invokes no "
            "subprocess; --confirm-run is required to invoke the Codex CLI. "
            "Advisory only and non-authoritative: Codex is never deterministic "
            "validation authority and never approves, blocks, merges, pushes, "
            "cleans up, deletes branches/worktrees, or changes lifecycle. Human "
            "final approval is always required."
        )
    )
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--repo-path", default=None)
    parser.add_argument("--worktree-path", default=None)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Explicitly request dry-run advisory contract. Dry-run is also the "
            "default when neither --dry-run nor --confirm-run is provided. "
            "Invokes no subprocess."
        ),
    )
    parser.add_argument(
        "--confirm-run",
        action="store_true",
        default=False,
        help=(
            "Explicitly opt in to invoking the Codex CLI once. Advisory only: "
            "Codex cannot approve, block, validate, merge, push, cleanup, delete "
            "branches/worktrees, or change lifecycle."
        ),
    )
    parser.add_argument(
        "--codex-command",
        default=None,
        help=(
            "Codex CLI command (default: codex). Only used with --confirm-run."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Positive timeout for the Codex CLI invocation (default: 300).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dry_run and args.confirm_run:
        print(
            "ERROR: --dry-run and --confirm-run are mutually exclusive",
            file=sys.stderr,
        )
        return 1

    if args.codex_command is not None and not args.confirm_run:
        print(
            "ERROR: --codex-command may only be used with --confirm-run",
            file=sys.stderr,
        )
        return 1

    confirm_run = bool(args.confirm_run)
    codex_command = args.codex_command if args.codex_command is not None else "codex"

    try:
        request = CodexAdvisoryReviewRequest(
            task_key=args.task_key,
            artifact_dir=Path(args.artifact_dir),
            repo_path=Path(args.repo_path) if args.repo_path else None,
            worktree_path=Path(args.worktree_path) if args.worktree_path else None,
            dry_run=not confirm_run,
            confirm_run=confirm_run,
            codex_command=codex_command,
            timeout_seconds=args.timeout_seconds,
        )
        result = generate_codex_advisory_review(request)
    except (ValueError, CodexAdvisoryReviewError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    mode = "confirm run" if confirm_run else "dry run"
    print(f"Codex advisory review ({mode}) generated:")
    print(f"- prompt: {result.prompt_path}")
    print(f"- json: {result.json_path}")
    print(f"- markdown: {result.markdown_path}")
    print(f"- review_status: {result.payload['review_status']}")
    print(f"- codex_cli_invoked: {result.payload['codex_cli_invoked']}")
    if result.stdout_path is not None:
        print(f"- stdout: {result.stdout_path}")
    if result.stderr_path is not None:
        print(f"- stderr: {result.stderr_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
