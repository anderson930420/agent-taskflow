"""Deterministic Ticket metadata derivation (SPEC §10.1).

The user supplies repository, prompt and priority only. Every other field —
task key, branch name, worktree path, artifact directory, base branch, and the
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

# Branch slugs stay short so that `<prefix><TASK_KEY>-<slug>` remains a
# comfortable ref name.
BRANCH_SLUG_MAX_CHARS = 40
BRANCH_SLUG_FALLBACK = "ticket"

# Task keys come from ONE global counter, zero-padded to 4 digits: `AT-0001`
# (human ruling on PR #195). The prefix is fixed; the registry's per-project
# `task_key_prefix` is not used for Ticket keys. Past 9999 the number grows.
TASK_KEY_PREFIX = "AT"
TASK_KEY_SEQUENCE_PAD = 4
FIRST_TASK_KEY_SEQUENCE = 1

_TASK_KEY_PATTERN = re.compile(rf"^{TASK_KEY_PREFIX}-(?P<sequence>\d+)$")
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


def format_task_key(sequence: int) -> str:
    """Return the global Ticket task key for one counter value."""
    if sequence < FIRST_TASK_KEY_SEQUENCE:
        raise ValueError(
            f"task key sequence must be >= {FIRST_TASK_KEY_SEQUENCE}"
        )
    return normalize_task_key(
        f"{TASK_KEY_PREFIX}-{sequence:0{TASK_KEY_SEQUENCE_PAD}d}"
    )


def parse_task_key_sequence(task_key: str) -> int | None:
    """Return the counter value inside an `AT-<digits>` key, else None.

    Keys of any other shape (`AT-GH-188`, `AT-MC-SMOKE`, `BJ-0001`) are not
    part of the counter and return None.
    """
    match = _TASK_KEY_PATTERN.match(task_key.strip())
    if match is None:
        return None
    return int(match.group("sequence"))


def derive_branch_name(branch_prefix: str, task_key: str, slug: str) -> str:
    """Return `<branch_prefix><TASK_KEY>-<slug>` (SPEC §17 example format).

    The task key is embedded, so branch names cannot collide between Tickets
    even when two Tickets produce an identical slug.
    """
    normalized_id = normalize_task_key(task_key)
    normalized_slug = slugify_branch_component(slug)
    branch = f"{branch_prefix.strip()}{normalized_id}-{normalized_slug}"
    if not _SAFE_BRANCH_NAME.match(branch) or ".." in branch or "//" in branch:
        raise ValueError(f"Derived branch name is not ref-safe: {branch!r}")
    return branch


def derive_worktree_path(worktrees_dir: str | Path, task_key: str) -> Path:
    """Return `<worktrees_dir>/<TASK_KEY>` (SPEC §9). String only."""
    return worktree_path_from_base(worktrees_dir, task_key)


def derive_artifact_dir(artifacts_root: str | Path, task_key: str) -> Path:
    """Return `<artifacts_root>/<TASK_KEY>` (SPEC §10.1). String only."""
    return artifact_dir_for(task_key, artifacts_root)


@dataclass(frozen=True)
class TicketDerivedMetadata:
    """Every Ticket field Python derives once the task key is allocated."""

    task_key: str
    branch: str
    worktree_path: Path
    artifact_dir: Path
    base_branch: str
    repo_path: Path
    github_repo: str | None


def derive_ticket_metadata(
    repository: TicketRepository,
    task_key: str,
    branch_slug: str,
) -> TicketDerivedMetadata:
    """Derive all Python-owned metadata for one Ticket."""
    normalized_id = normalize_task_key(task_key)
    return TicketDerivedMetadata(
        task_key=normalized_id,
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
    "FIRST_TASK_KEY_SEQUENCE",
    "TASK_KEY_PREFIX",
    "TASK_KEY_SEQUENCE_PAD",
    "TITLE_FALLBACK_MAX_CHARS",
    "TicketDerivedMetadata",
    "derive_artifact_dir",
    "derive_branch_name",
    "derive_ticket_metadata",
    "derive_worktree_path",
    "fallback_title_from_prompt",
    "format_task_key",
    "normalize_prompt",
    "parse_task_key_sequence",
    "slugify_branch_component",
]
