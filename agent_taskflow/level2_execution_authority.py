"""Shared Level 2 execution-authority and canonical Attempt policy.

Level 1 and historical task records retain their compatibility paths.  A task
with an explicit non-legacy ``TaskIdentityRecord`` is Level 2 and may cross a
legacy execution primitive only while it is being invoked inside the
authoritative :class:`ExecutionEngine` adapter.

This module is deliberately a policy/verification layer, not a second
lifecycle state machine.  Attempt state remains owned by :mod:`attempt_store`.
"""

from __future__ import annotations

from collections.abc import Collection
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from agent_taskflow.attempt_models import AttemptRecord
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import default_db_path
from agent_taskflow.tasks import normalize_task_key


EXECUTION_ENGINE_AUTHORITY = "execution_engine"
LEVEL2_TASK = "level2"
LEGACY_TASK = "legacy"
CANONICAL_HANDOFF_STATUSES = frozenset({"waiting_approval", "completed"})


class Level2ExecutionAuthorityError(RuntimeError):
    """Raised when Level 2 execution or Attempt authority cannot be proven."""


@dataclass(frozen=True)
class CanonicalAttemptVerification:
    """Verified canonical Attempt identity suitable for downstream handoff."""

    task_key: str
    task_id: str
    attempt: AttemptRecord

    def to_binding(self) -> dict[str, Any]:
        attempt = self.attempt
        return {
            "attempt_id": attempt.attempt_id,
            "task_id": attempt.task_id,
            "attempt_number": attempt.attempt_number,
            "status": attempt.status,
            "is_active": attempt.is_active,
            "execution_result": attempt.execution_result,
            "validation_result": attempt.validation_result,
            "is_legacy": attempt.is_legacy,
            "store_verified": True,
            "task_ownership_verified": True,
            "terminal_state_verified": True,
            "downstream_handoff_valid": True,
        }


_ENGINE_PRIMITIVE_AUTHORITY: ContextVar[tuple[str, str] | None] = ContextVar(
    "agent_taskflow_execution_engine_primitive_authority",
    default=None,
)


def _resolved_db_path(db_path: str | Path | None) -> Path:
    return Path(default_db_path() if db_path is None else db_path).expanduser().resolve()


def task_execution_level(
    db_path: str | Path | None,
    task_key: str,
) -> str:
    """Classify using the repository's canonical ``tasks.is_legacy`` marker.

    Databases that predate the additive Task/Attempt schema are historical by
    definition.  Other database errors are not converted into permission.
    """

    path = _resolved_db_path(db_path)
    normalized = normalize_task_key(task_key)
    if not path.exists():
        return LEGACY_TASK
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")
            }
    except sqlite3.DatabaseError as exc:
        raise Level2ExecutionAuthorityError(
            f"Could not classify task {normalized}: {exc}"
        ) from exc
    if "task_id" not in columns or "is_legacy" not in columns:
        return LEGACY_TASK
    try:
        identity = AttemptStore(path).get_task_identity(normalized)
    except sqlite3.DatabaseError as exc:
        raise Level2ExecutionAuthorityError(
            f"Could not read canonical identity for task {normalized}: {exc}"
        ) from exc
    return LEVEL2_TASK if identity is not None and not identity.is_legacy else LEGACY_TASK


def is_level2_task(db_path: str | Path | None, task_key: str) -> bool:
    return task_execution_level(db_path, task_key) == LEVEL2_TASK


def ensure_level2_task_identity(
    db_path: str | Path,
    task_key: str,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Persist the existing canonical Task classification for Level 2 work.

    Task admission passes ``connection`` so the task insert and this promotion
    commit as one transaction. Runtime callers omit it and keep the historical
    self-contained transaction behaviour; the classification outcome is
    identical either way, and both remain idempotent for an already-promoted
    task.
    """

    path = _resolved_db_path(db_path)
    store = AttemptStore(path)
    try:
        if connection is None:
            store.init_db()
            identity = store.get_task_identity(task_key)
            if identity is None:
                raise Level2ExecutionAuthorityError(
                    f"Canonical Task identity not found for {normalize_task_key(task_key)}"
                )
            if identity.is_legacy:
                identity = store.register_task_identity(
                    task_key,
                    task_class="canonical",
                    task_id=identity.task_id,
                    is_legacy=False,
                )
        else:
            identity = _promote_task_identity_in_connection(
                connection,
                task_key,
                store=store,
            )
    except Level2ExecutionAuthorityError:
        raise
    except (KeyError, ValueError, sqlite3.DatabaseError) as exc:
        raise Level2ExecutionAuthorityError(
            f"Could not bind {normalize_task_key(task_key)} to Level 2 identity: {exc}"
        ) from exc
    if identity.is_legacy:
        raise Level2ExecutionAuthorityError(
            f"Task {normalize_task_key(task_key)} remained legacy after Level 2 binding"
        )


def _promote_task_identity_in_connection(
    connection: sqlite3.Connection,
    task_key: str,
    *,
    store: AttemptStore,
) -> Any:
    """Promote one persisted task inside the caller's open transaction."""

    normalized = normalize_task_key(task_key)
    identity = AttemptStore.get_task_identity_in_connection(connection, normalized)
    if identity is None:
        # The row exists but predates stable identity columns; seed the legacy
        # identity first so promotion below has a stable task_id to keep.
        store._ensure_task_identity(connection, normalized)
        identity = AttemptStore.get_task_identity_in_connection(connection, normalized)
        if identity is None:
            raise Level2ExecutionAuthorityError(
                f"Canonical Task identity not found for {normalized}"
            )
    if identity.is_legacy:
        connection.execute(
            """
            UPDATE tasks
            SET task_class = 'canonical', is_legacy = 0, updated_at = ?
            WHERE task_key = ?
            """,
            (utc_now_iso(), normalized),
        )
        identity = AttemptStore.get_task_identity_in_connection(connection, normalized)
        if identity is None:
            raise Level2ExecutionAuthorityError(
                f"Canonical Task identity disappeared while promoting {normalized}"
            )
    return identity


@contextmanager
def execution_engine_primitive_authority(
    *,
    task_key: str,
    db_path: str | Path | None,
) -> Iterator[None]:
    """Authorize one internal legacy primitive call below ExecutionEngine."""

    binding = (normalize_task_key(task_key), str(_resolved_db_path(db_path)))
    token = _ENGINE_PRIMITIVE_AUTHORITY.set(binding)
    try:
        yield
    finally:
        _ENGINE_PRIMITIVE_AUTHORITY.reset(token)


def level2_direct_execution_error(
    *,
    task_key: str,
    db_path: str | Path | None,
    entrypoint: str,
    allow_engine_internal: bool = True,
) -> str | None:
    """Return a deterministic refusal for an unauthorized Level 2 primitive."""

    normalized = normalize_task_key(task_key)
    if not is_level2_task(db_path, normalized):
        return None
    expected = (normalized, str(_resolved_db_path(db_path)))
    if allow_engine_internal and _ENGINE_PRIMITIVE_AUTHORITY.get() == expected:
        return None
    return (
        f"Level 2 task {normalized} cannot execute through {entrypoint}; "
        "use the canonical ExecutionEngine path"
    )


def is_execution_engine_authority_callback(callback: Any) -> bool:
    """Accept only the repository's bound ExecutionEngine authority callback.

    A caller-controlled attribute or decorator is not an authority capability:
    both the bound owner and method identity must be the canonical scheduler
    bridge.  The import is intentionally lazy to avoid a module import cycle.
    """

    from agent_taskflow.scheduler_execution_engine_authority import (
        SchedulerExecutionEngineAuthority,
    )

    owner = getattr(callback, "__self__", None)
    target = getattr(callback, "__func__", None)
    return (
        isinstance(owner, SchedulerExecutionEngineAuthority)
        and target is SchedulerExecutionEngineAuthority.execute_from_runtime_handoff
    )


def list_canonical_attempt_ids(
    db_path: str | Path,
    task_key: str,
) -> set[str]:
    """Read all canonical Attempt IDs for one task from the authoritative store."""

    path = _resolved_db_path(db_path)
    if not path.exists():
        return set()
    try:
        return {
            attempt.attempt_id
            for attempt in AttemptStore(path).list_attempts(task_key)
        }
    except KeyError:
        # The engine may own initial canonical Task creation. Successful return
        # is still accepted only after exact post-execution verification.
        return set()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
            return set()
        raise Level2ExecutionAuthorityError(
            f"Could not read canonical Attempts for {normalize_task_key(task_key)}: {exc}"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise Level2ExecutionAuthorityError(
            f"Could not read canonical Attempts for {normalize_task_key(task_key)}: {exc}"
        ) from exc


def verify_canonical_attempt(
    *,
    db_path: str | Path,
    task_key: str,
    attempt_id: str,
    preexisting_attempt_ids: Collection[str] | None = None,
) -> CanonicalAttemptVerification:
    """Verify exact Task ownership, closure, and downstream eligibility.

    When ``preexisting_attempt_ids`` is supplied, the Attempt must have been
    created during the authoritative execution being verified.
    """

    normalized = normalize_task_key(task_key)
    resolved_attempt_id = str(attempt_id or "").strip()
    if not resolved_attempt_id:
        raise Level2ExecutionAuthorityError("canonical_attempt_id is required")
    store = AttemptStore(_resolved_db_path(db_path))
    try:
        identity = store.get_task_identity(normalized)
        attempt = store.get_attempt(resolved_attempt_id)
    except sqlite3.DatabaseError as exc:
        raise Level2ExecutionAuthorityError(
            f"Could not verify canonical Attempt {resolved_attempt_id}: {exc}"
        ) from exc
    if identity is None:
        raise Level2ExecutionAuthorityError(
            f"Canonical Task identity not found for {normalized}"
        )
    if attempt is None:
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt not found: {resolved_attempt_id}"
        )
    if attempt.task_id != identity.task_id:
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} belongs to another Task"
        )
    if preexisting_attempt_ids is not None and resolved_attempt_id in set(
        preexisting_attempt_ids
    ):
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} predates this authoritative execution"
        )
    if attempt.is_legacy:
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} is marked legacy"
        )
    if attempt.is_active or attempt.ended_at is None:
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} is not closed"
        )
    if attempt.status not in CANONICAL_HANDOFF_STATUSES:
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} status {attempt.status!r} "
            "is not valid for downstream handoff"
        )
    if attempt.execution_result != "completed":
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} execution did not complete"
        )
    if attempt.validation_result != "passed":
        raise Level2ExecutionAuthorityError(
            f"Canonical Attempt {resolved_attempt_id} validation did not pass"
        )
    return CanonicalAttemptVerification(
        task_key=normalized,
        task_id=identity.task_id,
        attempt=attempt,
    )


__all__ = [
    "CANONICAL_HANDOFF_STATUSES",
    "EXECUTION_ENGINE_AUTHORITY",
    "LEGACY_TASK",
    "LEVEL2_TASK",
    "CanonicalAttemptVerification",
    "Level2ExecutionAuthorityError",
    "execution_engine_primitive_authority",
    "ensure_level2_task_identity",
    "is_execution_engine_authority_callback",
    "is_level2_task",
    "level2_direct_execution_error",
    "list_canonical_attempt_ids",
    "task_execution_level",
    "verify_canonical_attempt",
]
