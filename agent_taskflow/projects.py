"""Project registry helpers for Agent Taskflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_taskflow.config import load_yaml_file


def load_projects_config(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load the project registry from config/projects.yaml."""
    data = load_yaml_file(path)
    projects = data.get("projects", {})
    if projects is None:
        return {}
    if not isinstance(projects, dict):
        raise ValueError("'projects' must be a mapping")
    return projects


def get_project_config(
    config: dict[str, dict[str, Any]],
    project_name: str,
) -> dict[str, Any]:
    """Return one project config or raise a clear error."""
    if project_name not in config:
        available = ", ".join(sorted(config.keys())) or "<none>"
        raise ValueError(
            f"Project {project_name!r} not found. Available projects: {available}"
        )

    project = config[project_name]
    if not isinstance(project, dict):
        raise ValueError(f"Project {project_name!r} config must be a mapping")
    return project


def resolve_project_config(path: str | Path, project_name: str) -> dict[str, Any]:
    """Load projects config and return one project config."""
    return get_project_config(load_projects_config(path), project_name)
