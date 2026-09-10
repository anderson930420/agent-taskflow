"""Read-only repository registry view for Ticket creation (SPEC §11).

Mission Control's repository dropdown and all Python-derived Ticket metadata
resolve through this module. It reads the existing project registry
(`config/projects.yaml`) via :mod:`agent_taskflow.projects` and never writes
to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.models import require_absolute_path
from agent_taskflow.projects import get_project_config, load_projects_config


DEFAULT_PROJECTS_CONFIG_PATH = Path("config/projects.yaml")

DEFAULT_BASE_BRANCH = "main"
DEFAULT_BRANCH_PREFIX = "task/"
DEFAULT_TICKET_PREFIX = "AT"

# Matches the local artifact root that GitHub Issue ingestion already uses when
# a project does not configure `artifacts_root`.
LOCAL_ARTIFACT_ROOT_PARTS = (".agent-taskflow", "artifacts")


class TicketRepositoryError(ValueError):
    """Raised when a repository cannot be resolved from the registry."""


@dataclass(frozen=True)
class TicketRepository:
    """One registry entry, normalized for Ticket metadata derivation."""

    repository: str
    repo_path: Path
    worktrees_dir: Path
    artifacts_root: Path
    base_branch: str
    branch_prefix: str
    ticket_prefix: str
    github_repo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view for the Mission Control dropdown."""
        return {
            "repository": self.repository,
            "repo_path": str(self.repo_path),
            "worktrees_dir": str(self.worktrees_dir),
            "artifacts_root": str(self.artifacts_root),
            "base_branch": self.base_branch,
            "branch_prefix": self.branch_prefix,
            "ticket_prefix": self.ticket_prefix,
            "github_repo": self.github_repo,
        }


def _optional_text(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text_or_default(config: dict[str, Any], key: str, default: str) -> str:
    return _optional_text(config, key) or default


def repository_from_config(name: str, config: dict[str, Any]) -> TicketRepository:
    """Normalize one raw registry entry into a :class:`TicketRepository`."""
    repository = require_non_empty(name, "repository")

    raw_repo_path = _optional_text(config, "repo_path")
    if raw_repo_path is None:
        raise TicketRepositoryError(
            f"Repository {repository!r} is missing repo_path in the registry"
        )
    try:
        repo_path = require_absolute_path(raw_repo_path, "repo_path")
    except ValueError as exc:
        raise TicketRepositoryError(
            f"Repository {repository!r} has an invalid repo_path: {exc}"
        ) from exc

    raw_worktrees_dir = _optional_text(config, "worktrees_dir")
    worktrees_dir = (
        require_absolute_path(raw_worktrees_dir, "worktrees_dir")
        if raw_worktrees_dir
        else repo_path / ".worktrees"
    )

    raw_artifacts_root = _optional_text(config, "artifacts_root")
    artifacts_root = (
        require_absolute_path(raw_artifacts_root, "artifacts_root")
        if raw_artifacts_root
        else repo_path.joinpath(*LOCAL_ARTIFACT_ROOT_PARTS)
    )

    return TicketRepository(
        repository=repository,
        repo_path=repo_path,
        worktrees_dir=worktrees_dir,
        artifacts_root=artifacts_root,
        base_branch=_text_or_default(config, "default_branch", DEFAULT_BASE_BRANCH),
        branch_prefix=_text_or_default(
            config,
            "branch_prefix",
            DEFAULT_BRANCH_PREFIX,
        ),
        ticket_prefix=_text_or_default(
            config,
            "task_key_prefix",
            DEFAULT_TICKET_PREFIX,
        ),
        github_repo=_optional_text(config, "github_repo"),
    )


def list_ticket_repositories(
    config_path: str | Path = DEFAULT_PROJECTS_CONFIG_PATH,
) -> list[TicketRepository]:
    """Return every registry entry, sorted by name, for the repo dropdown."""
    try:
        raw = load_projects_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        raise TicketRepositoryError(str(exc)) from exc

    repositories: list[TicketRepository] = []
    for name in sorted(raw):
        config = raw[name]
        if not isinstance(config, dict):
            raise TicketRepositoryError(
                f"Repository {name!r} config must be a mapping"
            )
        repositories.append(repository_from_config(name, config))
    return repositories


def resolve_ticket_repository(
    repository: str,
    config_path: str | Path = DEFAULT_PROJECTS_CONFIG_PATH,
) -> TicketRepository:
    """Return one normalized registry entry or raise TicketRepositoryError."""
    name = require_non_empty(repository, "repository")
    try:
        raw = load_projects_config(config_path)
        config = get_project_config(raw, name)
    except (FileNotFoundError, ValueError) as exc:
        raise TicketRepositoryError(str(exc)) from exc
    return repository_from_config(name, config)


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_BRANCH_PREFIX",
    "DEFAULT_PROJECTS_CONFIG_PATH",
    "DEFAULT_TICKET_PREFIX",
    "TicketRepository",
    "TicketRepositoryError",
    "list_ticket_repositories",
    "repository_from_config",
    "resolve_ticket_repository",
]
