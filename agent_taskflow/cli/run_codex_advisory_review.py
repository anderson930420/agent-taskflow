"""Packaged CLI for the dry-run Codex advisory reviewer contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_taskflow.codex_advisory_review import (
    CodexAdvisoryReviewError,
    CodexAdvisoryReviewRequest,
    generate_codex_advisory_review,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate dry-run Codex advisory review artifacts for an existing "
            "task artifact directory. Advisory only: Codex is never deterministic "
            "validation authority and never approves, blocks, merges, pushes, "
            "cleans up, deletes branches/worktrees, or changes lifecycle."
        )
    )
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--repo-path", default=None)
    parser.add_argument("--worktree-path", default=None)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run is the only supported mode in this milestone (default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = CodexAdvisoryReviewRequest(
            task_key=args.task_key,
            artifact_dir=Path(args.artifact_dir),
            repo_path=Path(args.repo_path) if args.repo_path else None,
            worktree_path=Path(args.worktree_path) if args.worktree_path else None,
            dry_run=True,
        )
        result = generate_codex_advisory_review(request)
    except (ValueError, CodexAdvisoryReviewError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Codex advisory review (dry run) generated:")
    print(f"- prompt: {result.prompt_path}")
    print(f"- json: {result.json_path}")
    print(f"- markdown: {result.markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
