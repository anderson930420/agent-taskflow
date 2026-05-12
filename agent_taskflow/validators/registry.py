"""Validator registry for built-in Agent Taskflow validators."""

from __future__ import annotations

from typing import Sequence

from agent_taskflow.validators.base import Validator
from agent_taskflow.validators.lint import LintValidator
from agent_taskflow.validators.openspec import OpenSpecValidator
from agent_taskflow.validators.policy import PolicyCheckValidator
from agent_taskflow.validators.pytest import PytestValidator
from agent_taskflow.validators.typecheck import TypecheckValidator


def _normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("validator name must not be empty")
    return normalized


def get_validator(
    name: str,
    *,
    python_bin: str = "python3",
    pytest_extra_args: Sequence[str] | None = None,
    openspec_bin: str = "openspec",
    openspec_args: Sequence[str] | None = None,
    scan_artifacts: bool = True,
) -> Validator:
    """Return a built-in validator by name without checking external binaries."""

    normalized = _normalize_name(name)

    if normalized == "pytest":
        return PytestValidator(
            python_bin=python_bin,
            extra_args=pytest_extra_args,
        )

    if normalized == "openspec":
        return OpenSpecValidator(
            openspec_bin=openspec_bin,
            args=openspec_args,
        )

    if normalized == "policy":
        return PolicyCheckValidator(
            scan_artifacts=scan_artifacts,
        )

    if normalized == "typecheck":
        return TypecheckValidator()

    if normalized == "lint":
        return LintValidator()

    raise ValueError(f"Unknown validator: {name!r}")


def list_validator_names() -> list[str]:
    """Return supported validator names."""

    return ["pytest", "openspec", "policy", "typecheck", "lint"]


__all__ = [
    "get_validator",
    "list_validator_names",
]
