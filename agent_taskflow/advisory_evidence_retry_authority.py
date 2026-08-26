"""In-process authority boundary for advisory-evidence recovery.

The recovery transition is an engine-owned exception to the forward-only
Attempt graph.  This context proves that a caller reached it through the
canonical recovery entry point before the final runtime store may create the
transaction-local SQLite authorization consumed by the lifecycle trigger.

It is deliberately a code-path boundary, not a claim that SQLite itself makes
this authority unforgeable.  The operator documentation records that limit.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from agent_taskflow.store import default_db_path
from agent_taskflow.tasks import normalize_task_key


_RECOVERY_AUTHORITY: ContextVar[tuple[str, str] | None] = ContextVar(
    "agent_taskflow_advisory_evidence_retry_authority",
    default=None,
)


def _binding(task_key: str, db_path: str | Path | None) -> tuple[str, str]:
    path = Path(default_db_path() if db_path is None else db_path).expanduser().resolve()
    return normalize_task_key(task_key), str(path)


@contextmanager
def advisory_evidence_retry_engine_authority(
    *,
    task_key: str,
    db_path: str | Path | None,
) -> Iterator[None]:
    """Authorize one internal recovery write through the canonical store."""

    token = _RECOVERY_AUTHORITY.set(_binding(task_key, db_path))
    try:
        yield
    finally:
        _RECOVERY_AUTHORITY.reset(token)


def require_advisory_evidence_retry_engine_authority(
    *,
    task_key: str,
    db_path: str | Path | None,
) -> None:
    """Fail closed unless the canonical recovery entry point authorized this call."""

    if _RECOVERY_AUTHORITY.get() != _binding(task_key, db_path):
        raise RuntimeError(
            "canonical engine-authorized advisory recovery path required"
        )


__all__ = [
    "advisory_evidence_retry_engine_authority",
    "require_advisory_evidence_retry_engine_authority",
]
