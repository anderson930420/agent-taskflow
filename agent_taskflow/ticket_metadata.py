"""Deterministic Ticket metadata derivation (SPEC §10.1).

The user supplies repository, prompt and priority only. Every other field —
Task ID, branch name, worktree path, artifact directory, base branch, and the
deterministic title fallback — is derived here by Python.

Path and branch values are *strings only*. Nothing in this module touches
Git or the filesystem: worktree creation is Step 2 territory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.artifacts import artifact_dir_for
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_repositories import TicketRepository
from agent_taskflow.worktree import worktree_path_from_base


# SPEC §10.1 title fallback: first 60 displayable characters of the
# whitespace-normalized prompt.
TITLE_FALLBACK_MAX_CHARS = 60

# Branch slugs stay short so that `<prefix><TICKET_ID>-<slug>` remains a
# comfortable ref name.
BRANCH_SLUG_MAX_CHARS = 40
BRANCH_SLUG_FALLBACK = "ticket"

# Zero-padded width of the numeric part of a Task ID (`AT-101`, `AT-098`).
# Sequences past the padding width simply grow (`AT-1000`).
TICKET_SEQUENCE_PAD = 3
FIRST_TICKET_SEQUENCE = 1

_TICKET_ID_PATTERN = re.compile(r"^(?P<prefix>[A-Za-z0-9]+)-(?P<sequence>\d+)$")
_UNSAFE_BRANCH_CHARS = re.compile(r"[^a-z0-9]+")
_SAFE_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def normalize_prompt(prompt: str) -> str:
    """Collapse whitespace and drop non-displayable characters.

    Whitespace runs (including newlines and tabs) become single spaces, and
    control / non-printable characters are removed. This is the shared
    normalization behind both the stored prompt and the §10.1 title fallback.
    """
    displayable = "".join(
        char for char in prompt if char.isspace() or char.isprintable()
    )
    return " ".join(displayable.split())


def fallback_title_from_prompt(prompt: str) -> str:
    """Return the SPEC §10.1 deterministic title fallback."""
    normalized = normalize_prompt(prompt)
    if not normalized:
        raise ValueError("prompt must contain at least one displayable character")
    return normalized[:TITLE_FALLBACK_MAX_CHARS]


def slugify_branch_component(value: str) -> str:
    """Return a lowercase, hyphen-separated, ref-safe branch component."""
    lowered = normalize_prompt(value).lower()
    slug = _UNSAFE_BRANCH_CHARS.sub("-", lowered).strip("-")
    slug = slug[:BRANCH_SLUG_MAX_CHARS].strip("-")
    return slug or BRANCH_SLUG_FALLBACK


def format_ticket_id(prefix: str, sequence: int) -> str:
    """Return the Task ID for one registry prefix and counter value."""
    normalized_prefix = require_non_empty(prefix, "ticket_prefix")
    if not normalized_prefix.isalnum():
        raise ValueError(
            f"ticket_prefix must be alphanumeric: {prefix!r}"
        )
    if sequence < FIRST_TICKET_SEQUENCE:
        raise ValueError(f"ticket sequence must be >= {FIRST_TICKET_SEQUENCE}")
    return normalize_task_key(
        f"{normalized_prefix}-{sequence:0{TICKET_SEQUENCE_PAD}d}"
    )


def parse_ticket_sequence(ticket_id: str, prefix: str) -> int | None:
    """Return the counter value inside a Task ID, or None if it does not match."""
    match = _TICKET_ID_PATTERN.match(ticket_id.strip())
    if match is None or match.group("prefix") != prefix:
        return None
    return int(match.group("sequence"))


def derive_branch_name(branch_prefix: str, ticket_id: str, slug: str) -> str:
    """Return `<branch_prefix><TICKET_ID>-<slug>` (SPEC §17 example format).

    The Task ID is embedded, so branch names cannot collide between Tickets
    even when two Tickets produce an identical slug.
    """
    normalized_id = normalize_task_key(ticket_id)
    normalized_slug = slugify_branch_component(slug)
    branch = f"{branch_prefix.strip()}{normalized_id}-{normalized_slug}"
    if not _SAFE_BRANCH_NAME.match(branch) or ".." in branch or "//" in branch:
        raise ValueError(f"Derived branch name is not ref-safe: {branch!r}")
    return branch


def derive_worktree_path(worktrees_dir: str | Path, ticket_id: str) -> Path:
    """Return `<worktrees_dir>/<TICKET_ID>` (SPEC §9). String only."""
    return worktree_path_from_base(worktrees_dir, ticket_id)


def derive_artifact_dir(artifacts_root: str | Path, ticket_id: str) -> Path:
    """Return `<artifacts_root>/<TICKET_ID>` (SPEC §10.1). String only."""
    return artifact_dir_for(ticket_id, artifacts_root)


@dataclass(frozen=True)
class TicketDerivedMetadata:
    """Every Ticket field Python derives once the Task ID is allocated."""

    ticket_id: str
    branch: str
    worktree_path: Path
    artifact_dir: Path
    base_branch: str
    repo_path: Path
    github_repo: str | None


def derive_ticket_metadata(
    repository: TicketRepository,
    ticket_id: str,
    branch_slug: str,
) -> TicketDerivedMetadata:
    """Derive all Python-owned metadata for one Ticket."""
    normalized_id = normalize_task_key(ticket_id)
    return TicketDerivedMetadata(
        ticket_id=normalized_id,
        branch=derive_branch_name(
            repository.branch_prefix,
            normalized_id,
            branch_slug,
        ),
        worktree_path=derive_worktree_path(repository.worktrees_dir, normalized_id),
        artifact_dir=derive_artifact_dir(repository.artifacts_root, normalized_id),
        base_branch=repository.base_branch,
        repo_path=repository.repo_path,
        github_repo=repository.github_repo,
    )


__all__ = [
    "BRANCH_SLUG_FALLBACK",
    "BRANCH_SLUG_MAX_CHARS",
    "FIRST_TICKET_SEQUENCE",
    "TICKET_SEQUENCE_PAD",
    "TITLE_FALLBACK_MAX_CHARS",
    "TicketDerivedMetadata",
    "derive_artifact_dir",
    "derive_branch_name",
    "derive_ticket_metadata",
    "derive_worktree_path",
    "fallback_title_from_prompt",
    "format_ticket_id",
    "normalize_prompt",
    "parse_ticket_sequence",
    "slugify_branch_component",
]
