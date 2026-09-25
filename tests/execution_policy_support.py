"""Shared helpers for tests that need a V1 execution policy (RULINGS 67).

Not a test module. A registry is written into a temporary directory and the
resolver is pointed at it by patching
``agent_taskflow.execution_policy.PROJECTS_REGISTRY_PATH`` — the same absolute
path every production entry point reads. The executor is
``tests/fake_claude_executable.py``, so no test ever calls a real model.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import sys
from typing import Any, Iterator
from unittest import mock

import yaml

import agent_taskflow.execution_policy as execution_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude_executable.py"
TEST_MODEL = "claude-test-model"
TEST_EFFORT = "medium"


def policy_block(
    *,
    executor: str = "claude-code",
    argv: list[str] | None = None,
    implementation_validators: list[dict[str, Any]] | None = None,
    integration_validators: list[dict[str, Any]] | None = None,
    policy_version: str = "test-1",
    timeout_seconds: int | None = 120,
    **overrides: Any,
) -> dict[str, Any]:
    """A valid ``execution:`` block whose executor is the fake claude script."""
    block: dict[str, Any] = {
        "executor": executor,
        "argv": argv
        if argv is not None
        else [sys.executable, str(FAKE_CLAUDE), "--model", "{model}", "--effort", "{effort}"],
        "model": TEST_MODEL,
        "effort": TEST_EFFORT,
        "timeout_seconds": timeout_seconds,
        "permission_profile": "test-bounded-implementer",
        "policy_version": policy_version,
        "implementation_validators": implementation_validators
        if implementation_validators is not None
        else [{"name": "pytest", "timeout_seconds": 60}],
        "integration_validators": integration_validators
        if integration_validators is not None
        else [{"name": "unit", "command": ["python3", "-c", "pass"], "timeout_seconds": 60}],
    }
    if timeout_seconds is None:
        del block["timeout_seconds"]
    block.update(overrides)
    return block


def project_entry(
    repo_path: str | Path,
    *,
    execution: dict[str, Any] | None = None,
    github_repo: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"repo_path": str(repo_path), "default_branch": "main"}
    if github_repo is not None:
        entry["github_repo"] = github_repo
    if execution is not None:
        entry["execution"] = execution
    return entry


def write_registry(path: str | Path, projects: dict[str, dict[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump({"projects": projects}, sort_keys=True), encoding="utf-8")
    return target


def use_registry(path: str | Path) -> Any:
    """Patch the resolver's registry path; returns the started patcher."""
    patcher = mock.patch.object(execution_policy, "PROJECTS_REGISTRY_PATH", Path(path))
    patcher.start()
    return patcher


@contextmanager
def registry(path: str | Path, projects: dict[str, dict[str, Any]]) -> Iterator[Path]:
    write_registry(path, projects)
    patcher = use_registry(path)
    try:
        yield Path(path)
    finally:
        patcher.stop()


def integration_entries(specs: Any) -> list[dict[str, Any]]:
    """Policy ``integration_validators`` entries for IntegrationValidatorSpecs."""
    return [
        {"name": spec.name, "command": list(spec.command), "timeout_seconds": spec.timeout_seconds}
        for spec in specs
    ]


def release_tree(dest: str | Path, registry_path: str | Path) -> Path:
    """A disposable copy of the package and scripts with its own registry.

    A CLI started as a subprocess cannot be patched, and the resolver reads the
    registry anchored to the package it runs from, so a subprocess test runs a
    copy whose ``config/projects.yaml`` is the test's registry — the same shape
    as a deployed release directory.
    """
    root = Path(dest)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("agent_taskflow", "scripts"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=ignore)
    install_release_registry(root, registry_path)
    return root


def install_release_registry(release: str | Path, registry_path: str | Path) -> None:
    target = Path(release) / "config" / "projects.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(registry_path, target)


def policy_for(project: str) -> execution_policy.ExecutionPolicy:
    return execution_policy.resolve_execution_policy(project)


__all__ = [
    "FAKE_CLAUDE",
    "REPO_ROOT",
    "install_release_registry",
    "integration_entries",
    "release_tree",
    "TEST_EFFORT",
    "TEST_MODEL",
    "policy_block",
    "policy_for",
    "project_entry",
    "registry",
    "use_registry",
    "write_registry",
]
