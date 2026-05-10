"""Task-key helpers for Agent Taskflow."""

from __future__ import annotations

import re

ALLOWED_TASK_KEY_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")


def normalize_task_key(task_key: str) -> str:
    """Normalize and validate a task key."""
    key = task_key.strip()
    if not key:
        raise ValueError("Task key must not be empty")
    if not ALLOWED_TASK_KEY_CHARS.fullmatch(key):
        raise ValueError(
            f"Task key {task_key!r} contains unsafe characters. "
            "Allowed: [A-Za-z0-9._-]+"
        )
    return key
