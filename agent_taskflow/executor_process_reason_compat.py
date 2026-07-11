"""Extend the closed runtime reason taxonomy for PR-7 process outcomes."""

from __future__ import annotations

import agent_taskflow.lifecycle_control as lifecycle_control

EXECUTOR_PROCESS_RUNTIME_REASON_CODES = frozenset(
    {
        "executor_process_exit_unverified",
        "executor_descendant_cleanup",
        "executor_process_identity_mismatch",
    }
)


def install_executor_process_reason_compat() -> None:
    lifecycle_control.RUNTIME_REASON_CODES = frozenset(
        lifecycle_control.RUNTIME_REASON_CODES | EXECUTOR_PROCESS_RUNTIME_REASON_CODES
    )


__all__ = [
    "EXECUTOR_PROCESS_RUNTIME_REASON_CODES",
    "install_executor_process_reason_compat",
]
