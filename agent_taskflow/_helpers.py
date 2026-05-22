"""Small internal helpers shared across Agent Taskflow modules."""

from __future__ import annotations

from collections.abc import Iterable


def require_non_empty(value: str, field_name: str) -> str:
    """Return stripped text or raise ValueError with the standard message."""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    """Return values with duplicates removed, preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def dedupe_non_empty_preserve_order(values: Iterable[str]) -> list[str]:
    """Return non-empty values with duplicates removed, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = [
    "dedupe_non_empty_preserve_order",
    "dedupe_preserve_order",
    "require_non_empty",
]
