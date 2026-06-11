"""Source-level tests for minimal Python packaging metadata."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def _pyproject() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _runtime_requirements() -> list[str]:
    return [
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_pyproject_declares_minimal_build_backend() -> None:
    data = _pyproject()
    build_system = data["build-system"]

    assert build_system["build-backend"] == "setuptools.build_meta"
    assert "setuptools>=69" in build_system["requires"]
    assert "wheel" in build_system["requires"]


def test_project_metadata_matches_current_distribution_contract() -> None:
    project = _pyproject()["project"]

    assert project["name"] == "agent-taskflow"
    assert project["version"] == "0.1.0"
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.12"
    assert "human-gated AI engineering workflows" in project["description"]


def test_project_dependencies_match_runtime_requirements() -> None:
    project = _pyproject()["project"]

    assert project["dependencies"] == _runtime_requirements()


def test_setuptools_package_discovery_is_limited_to_runtime_modules() -> None:
    tool = _pyproject()["tool"]
    setuptools = tool["setuptools"]
    package_find = setuptools["packages"]["find"]

    assert package_find["include"] == ["agent_taskflow*", "scripts*"]


def test_console_script_entry_points_are_importable_callables() -> None:
    scripts = _pyproject()["project"]["scripts"]
    assert scripts == {
        "agent-taskflow-local-validation": "scripts.run_local_validation:main",
        "agent-taskflow-github-issue-one-task-automation": (
            "scripts.run_github_issue_one_task_automation:main"
        ),
        "agent-taskflow-github-issue-one-task-scheduler-tick": (
            "scripts.run_github_issue_one_task_scheduler_tick:main"
        ),
    }

    for target in scripts.values():
        module_name, function_name = target.split(":", 1)
        module = importlib.import_module(module_name)
        entry_point = getattr(module, function_name)
        assert callable(entry_point)


def test_scripts_directory_is_importable_package() -> None:
    module = importlib.import_module("scripts")

    assert module.__doc__
    assert module.__file__ is not None
    assert Path(module.__file__).name == "__init__.py"
