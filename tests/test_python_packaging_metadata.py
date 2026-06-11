"""Source-level tests for minimal Python packaging metadata."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_console_entry_points_use_agent_taskflow_cli_modules() -> None:
    scripts = _pyproject()["project"]["scripts"]

    for target in scripts.values():
        module_name, function_name = target.split(":", 1)
        assert module_name.startswith("agent_taskflow.cli.")
        assert callable(getattr(importlib.import_module(module_name), function_name))


def test_package_discovery_excludes_top_level_scripts_package() -> None:
    package_find = _pyproject()["tool"]["setuptools"]["packages"]["find"]

    assert package_find["include"] == ["agent_taskflow*"]
    assert not (REPO_ROOT / "scripts" / "__init__.py").exists()
