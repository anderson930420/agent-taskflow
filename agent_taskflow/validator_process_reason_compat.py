"""Register PR-9 validator process lifecycle reason codes."""

from __future__ import annotations

import agent_taskflow.lifecycle_control as lifecycle_control

VALIDATOR_PROCESS_REASON_CODES = frozenset(
    {
        "validator_launch_allocated",
        "validator_launch_preflight_failed",
        "validator_process_start_failed",
        "validator_process_started",
        "validator_process_exited",
        "validator_timeout",
        "validator_descendant_cleanup",
        "validator_process_sigterm_sent",
        "validator_process_sigkill_sent",
        "validator_process_exit_verified",
        "validator_process_exit_unverified",
        "validator_process_identity_mismatch",
    }
)


def install_validator_process_reason_compat() -> None:
    lifecycle_control.RUNTIME_REASON_CODES = frozenset(
        lifecycle_control.RUNTIME_REASON_CODES | VALIDATOR_PROCESS_REASON_CODES
    )


__all__ = [
    "VALIDATOR_PROCESS_REASON_CODES",
    "install_validator_process_reason_compat",
]
