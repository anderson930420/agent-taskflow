"""The V1 prompt writer: a Ticket's prompt becomes its Attempt's prompt file (RULINGS 67).

A Ticket is prompt-first (SPEC §11): the text the user typed in Mission Control
is the whole task. The dispatcher calls :func:`write_ticket_implementation_prompt`
after the claim, so the file lands in the claimed Attempt's own artifact root
and is never shared with another Attempt. The file holds the prompt verbatim;
the executor adapter wraps it in its own bounded-implementer framing, and the
mission contract's goal is the same text.

This module only reads the Ticket row and writes one file. It runs nothing and
decides nothing.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from agent_taskflow.atomic_write import atomic_write_text
from agent_taskflow.executors.implementation_prompt import IMPLEMENTATION_PROMPT_FILENAME
from agent_taskflow.models import require_absolute_path
from agent_taskflow.tasks import normalize_task_key


class TicketPromptError(ValueError):
    """The Ticket has no usable prompt."""


def read_ticket_prompt(db_path: str | Path, task_key: str) -> str:
    """Return the Ticket's stored prompt. Read-only; raises when it is empty."""
    path = require_absolute_path(db_path, "db_path")
    normalized = normalize_task_key(task_key)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT prompt FROM tasks WHERE task_key = ?", (normalized,)).fetchone()
    prompt = row[0] if row is not None else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise TicketPromptError(f"Ticket {normalized} has no prompt")
    return prompt


def write_ticket_implementation_prompt(prompt: str, attempt_artifact_root: str | Path) -> Path:
    """Write ``prompt`` verbatim as the Attempt's ``implementation_prompt.md``."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise TicketPromptError("a Ticket prompt must not be empty")
    root = require_absolute_path(attempt_artifact_root, "attempt_artifact_root")
    root.mkdir(parents=True, exist_ok=True)
    target = root / IMPLEMENTATION_PROMPT_FILENAME
    atomic_write_text(target, prompt)
    return target


__all__ = [
    "TicketPromptError",
    "read_ticket_prompt",
    "write_ticket_implementation_prompt",
]
