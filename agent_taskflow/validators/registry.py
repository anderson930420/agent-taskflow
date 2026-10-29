"""Validator registry for built-in Agent Taskflow validators."""

from __future__ import annotations

from typing import Sequence

from agent_taskflow.validators.base import Validator
from agent_taskflow.validators.affected_tests import AffectedTestsValidator
from agent_taskflow.validators.compile import CompileValidator
from agent_taskflow.validators.pytest import PytestValidator


def _normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("validator name must not be empty")
    return normalized


def get_validator(
    name: str,
    *,
    python_bin: str | None = None,
    pytest_extra_args: Sequence[str] | None = None,
) -> Validator:
    """Return a built-in validator by name without checking external binaries."""

    normalized = _normalize_name(name)

    if normalized == "pytest":
        return PytestValidator(
            python_bin=python_bin,
            extra_args=pytest_extra_args,
        )

    if normalized == "affected-tests":
        return AffectedTestsValidator(python_bin=python_bin)

    if normalized == "compile":
        return CompileValidator(python_bin=python_bin)

    raise ValueError(f"Unknown validator: {name!r}")


def list_validator_names() -> list[str]:
    """Return supported validator names."""

    return ["pytest", "compile", "affected-tests"]


__all__ = [
    "get_validator",
    "list_validator_names",
]
