"""The V0 supported-surface refusal (RULINGS 74, 80, 81).

V0 has one execution path: ``POST /api/tickets`` -> the execution tick -> the
integration tick (``docs/v0-supported-surface.md``). An entrypoint outside that
path that could still touch a V1 Ticket calls :func:`require_supported_in_v0`
before it writes anything or starts a subprocess, in every mode, and renders
:class:`UnsupportedInV0` in its own native refusal form (a JSON result with exit
2, an HTTP 409, or its blocked/refused result). Legacy tasks pass unchanged.

The entrypoint guards are the invariant. That no Ticket is in some state today
is migration-state evidence, not a reason to skip a guard.

It reuses D1's :func:`~agent_taskflow.ticket_lifecycle.legacy_entrypoint_ticket_refusal`
unchanged, so it has the same fail-closed semantics and message: no database
path, or a database that cannot be read, is refused; a database that does not
exist yet holds no Ticket and passes.
"""

from __future__ import annotations

from pathlib import Path

from agent_taskflow.ticket_lifecycle import (
    LEGACY_ENTRYPOINT_REFUSED,
    legacy_entrypoint_ticket_refusal,
)


class UnsupportedInV0(RuntimeError):
    """A non-V0 entrypoint refused a V1 Ticket before doing anything (RULINGS 74, 80)."""

    reason_code = LEGACY_ENTRYPOINT_REFUSED


def require_supported_in_v0(
    db_path: str | Path | None,
    task_key: str,
    *,
    entrypoint: str,
) -> None:
    """Raise :class:`UnsupportedInV0` when ``task_key`` is (or may be) a V1 Ticket."""
    refusal = legacy_entrypoint_ticket_refusal(db_path, task_key, entrypoint=entrypoint)
    if refusal is not None:
        raise UnsupportedInV0(refusal)


__all__ = ["UnsupportedInV0", "require_supported_in_v0"]
