"""Internal shared runtime context validation helpers.

These helpers enforce consistent context invariants for executor and
validator runtime contexts: non-empty strings, positive timeouts, and
rejection of secret-like env keys. They are internal-facing and not part
of the public agent-taskflow API.
"""

from __future__ import annotations


SECRET_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)


def require_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def validate_timeout(timeout_seconds: int | None) -> int | None:
    if timeout_seconds is None:
        return None
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive when provided")
    return timeout_seconds


def validate_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None

    normalized: dict[str, str] = {}
    for key, value in env.items():
        env_key = require_non_empty(str(key), "env key")
        if not isinstance(value, str):
            raise TypeError(f"env value for {env_key!r} must be a string")

        upper_key = env_key.upper()
        if any(marker in upper_key for marker in SECRET_ENV_MARKERS):
            raise ValueError(
                f"env must not include secret-like key: {env_key!r}"
            )

        normalized[env_key] = value

    return normalized
