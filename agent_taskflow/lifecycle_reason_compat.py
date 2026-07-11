"""Extend PR-6's closed reason taxonomy with stable PR-4 compatibility codes."""

from __future__ import annotations

import agent_taskflow.lifecycle_control as lifecycle_control

CANONICAL_COMPATIBILITY_REASON_CODES = frozenset(
    {
        "canonical_runtime_pickup_claimed",
        "canonical_runtime_waiting_approval",
        "canonical_runtime_completed",
        "canonical_runtime_canceled",
        "canonical_runtime_blocked",
        "canonical_runtime_released",
    }
)


def install_lifecycle_reason_compat() -> None:
    """Keep public PR-4/PR-5 stores valid under the PR-6 release validator."""
    lifecycle_control.RUNTIME_REASON_CODES = frozenset(
        lifecycle_control.RUNTIME_REASON_CODES | CANONICAL_COMPATIBILITY_REASON_CODES
    )


__all__ = [
    "CANONICAL_COMPATIBILITY_REASON_CODES",
    "install_lifecycle_reason_compat",
]
