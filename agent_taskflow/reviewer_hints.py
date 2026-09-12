"""Reviewer hints for integrated PRs (§26.1, §38).

Hints are attention routing, not correctness decisions (§38). They carry no
verdict, no severity that gates anything, and nothing in Step 2 branches on
them — they exist so a human reviewer knows where to look first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


__all__ = [
    "HINT_AI_CONFLICT_RESOLUTION",
    "HINT_LARGE_DIFF",
    "HINT_MIGRATION_CHANGED",
    "HINT_OVERLAPPING_FILES",
    "HINT_REINTEGRATED",
    "HINT_REINTEGRATED_REPEATEDLY",
    "HINT_SIGNIFICANT_BEHIND_COUNT",
    "HINT_VALIDATOR_RESULT_CHANGED",
    "ReviewerHint",
    "build_reviewer_hints",
    "render_hints_markdown",
    "render_reintegration_hint_block",
]


HINT_AI_CONFLICT_RESOLUTION = "ai_conflict_resolution"
HINT_REINTEGRATED = "reintegrated"
HINT_REINTEGRATED_REPEATEDLY = "reintegrated_repeatedly"
HINT_OVERLAPPING_FILES = "overlapping_files"
HINT_SIGNIFICANT_BEHIND_COUNT = "significant_behind_count"
HINT_LARGE_DIFF = "large_diff"
HINT_MIGRATION_CHANGED = "migration_changed"
HINT_VALIDATOR_RESULT_CHANGED = "validator_result_changed"

REPEATED_REINTEGRATION_THRESHOLD = 3
SIGNIFICANT_BEHIND_COUNT = 10
LARGE_DIFF_FILE_COUNT = 30

_MIGRATION_MARKERS = ("migration", "migrations/", "alembic", "schema.sql")


@dataclass(frozen=True)
class ReviewerHint:
    """One attention-routing hint. Deliberately verdict-free."""

    code: str
    message: str


def build_reviewer_hints(
    *,
    ai_resolved_conflict: bool = False,
    reintegration_count: int = 0,
    behind_count: int = 0,
    changed_files: Sequence[str] = (),
    changed_file_count: int | None = None,
    overlapping_files: Sequence[str] = (),
    migration_changed: bool | None = None,
    validator_result_changed: bool = False,
) -> list[ReviewerHint]:
    """Return the §38 hints that apply to one integration result."""
    hints: list[ReviewerHint] = []

    if ai_resolved_conflict:
        hints.append(
            ReviewerHint(
                HINT_AI_CONFLICT_RESOLUTION,
                "AI-assisted conflict resolution occurred.",
            )
        )

    if reintegration_count >= REPEATED_REINTEGRATION_THRESHOLD:
        hints.append(
            ReviewerHint(
                HINT_REINTEGRATED_REPEATEDLY,
                f"Re-integrated {reintegration_count} times.",
            )
        )
    elif reintegration_count >= 1:
        hints.append(
            ReviewerHint(
                HINT_REINTEGRATED,
                "Re-integrated after target branch advanced.",
            )
        )

    if overlapping_files:
        hints.append(
            ReviewerHint(
                HINT_OVERLAPPING_FILES,
                "Files overlap with recently merged Ticket: "
                + ", ".join(sorted(overlapping_files)),
            )
        )

    if behind_count >= SIGNIFICANT_BEHIND_COUNT:
        hints.append(
            ReviewerHint(
                HINT_SIGNIFICANT_BEHIND_COUNT,
                f"Branch had significant behind count ({behind_count}).",
            )
        )

    file_count = changed_file_count if changed_file_count is not None else len(changed_files)
    if file_count >= LARGE_DIFF_FILE_COUNT:
        hints.append(
            ReviewerHint(HINT_LARGE_DIFF, f"Large diff ({file_count} files changed).")
        )

    touches_migration = migration_changed
    if touches_migration is None:
        touches_migration = any(
            marker in path.lower() for path in changed_files for marker in _MIGRATION_MARKERS
        )
    if touches_migration:
        hints.append(ReviewerHint(HINT_MIGRATION_CHANGED, "Migration changed."))

    if validator_result_changed:
        hints.append(
            ReviewerHint(
                HINT_VALIDATOR_RESULT_CHANGED,
                "Integration validator result changed after target update.",
            )
        )

    return hints


def render_reintegration_hint_block(
    *,
    previous_base_sha: str | None,
    current_base_sha: str | None,
    trigger: str | None = None,
) -> str:
    """Render the §26.1 re-integration block for the PR body."""
    lines = [
        "⚠ Re-integrated after target branch advanced.",
        "",
        "Previous base:",
        previous_base_sha or "unknown",
        "",
        "Current base:",
        current_base_sha or "unknown",
    ]
    if trigger:
        lines.extend(["", "Trigger:", trigger])
    return "\n".join(lines)


def render_hints_markdown(hints: Sequence[ReviewerHint]) -> str:
    """Render hints as a PR-body fragment. Empty in, empty out."""
    if not hints:
        return ""
    return "\n".join(f"⚠ {hint.message}" for hint in hints)
