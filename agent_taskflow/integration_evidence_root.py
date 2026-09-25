"""Where one integration run writes its evidence (Level 2 M2 Exit Gate row 3).

Roadmap §2.4 asks for every artifact to carry the Attempt's identity, under
``<task-key>/<attempt-id>/``. Integration has no Attempt of its own: it runs
after the producing Attempt released its claim, and it is handed that Attempt
through the queue entry (:func:`~agent_taskflow.integration_handoff.
resolve_producer_attempt_binding`). This module turns that binding into the
directory the run writes to:

* a bound producer whose recorded ``attempts.artifact_root`` is an existing,
  real directory gets its evidence there, with the same relative layout the
  task level used (``validation-runs/<uuid>/`` and ``integration/``);
* everything else — a manual, watcher or legacy run, a superseded or refused
  producer, an Attempt row without a usable root — keeps the task-level
  artifact directory, and the result says exactly why.

Allocating an Attempt's resources also points ``tasks.artifact_dir`` at that
Attempt's root (``attempt_resources.allocate``), so the task's recorded
directory is usually the *latest* Attempt's root, not the producer's and not
the task level. A run without a producer therefore never writes there: when
the recorded directory is an Attempt root, the fallback is that Attempt's
recorded ``artifact_base_root``, so unbound evidence is not filed under an
Attempt that did not produce it. If that read-only lookup fails, the recorded
directory is used as it is and the result says so
(``task_level_reason_code``); it never happens silently.

The root is read from the Attempt row, never derived from the task's active
pointer or its newest Attempt, and never created here: a missing root is
reported, not recreated. Nothing already written is moved.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.integration_handoff import ProducerAttemptBinding


__all__ = [
    "IntegrationEvidenceRoot",
    "REASON_ATTEMPT_ROOT",
    "REASON_ATTEMPT_ROOT_UNAVAILABLE",
    "REASON_ATTEMPT_ROOT_UNSAFE",
    "REASON_ATTEMPT_UNREADABLE",
    "REASON_NO_ATTEMPT_ROOT",
    "REASON_NO_PRODUCER",
    "SCOPE_ATTEMPT",
    "SCOPE_NONE",
    "SCOPE_TASK",
    "TASK_LEVEL_FROM_ATTEMPT_BASE",
    "TASK_LEVEL_RECORDED",
    "TASK_LEVEL_UNRESOLVED",
    "resolve_integration_evidence_root",
]


SCOPE_ATTEMPT = "attempt"
SCOPE_TASK = "task"
SCOPE_NONE = "none"

REASON_ATTEMPT_ROOT = "producer_attempt_artifact_root"
REASON_NO_PRODUCER = "no_producer_attempt"
REASON_ATTEMPT_UNREADABLE = "producer_attempt_unreadable"
REASON_NO_ATTEMPT_ROOT = "producer_attempt_has_no_artifact_root"
REASON_ATTEMPT_ROOT_UNSAFE = "producer_attempt_artifact_root_unsafe"
REASON_ATTEMPT_ROOT_UNAVAILABLE = "producer_attempt_artifact_root_unavailable"

# How a task-level result chose its directory.
TASK_LEVEL_RECORDED = "task_artifact_dir_is_no_attempt_root"
TASK_LEVEL_FROM_ATTEMPT_BASE = "task_artifact_dir_is_attempt_root"
TASK_LEVEL_UNRESOLVED = "task_level_directory_unresolved"


@dataclass(frozen=True)
class IntegrationEvidenceRoot:
    """The directory an integration run writes to, and why."""

    path: Path | None
    scope: str
    reason_code: str
    reason: str
    attempt_id: str | None = None
    task_artifact_dir: Path | None = None
    # Set when the task's recorded directory was this Attempt's root and the
    # task level was taken from its recorded artifact_base_root instead.
    task_artifact_dir_attempt_id: str | None = None
    # How the task-level directory was decided, for a task-level result only:
    # TASK_LEVEL_RECORDED, TASK_LEVEL_FROM_ATTEMPT_BASE or TASK_LEVEL_UNRESOLVED.
    task_level_reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": None if self.path is None else str(self.path),
            "scope": self.scope,
            "attempt_id": self.attempt_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "task_artifact_dir": (
                None if self.task_artifact_dir is None else str(self.task_artifact_dir)
            ),
            "task_artifact_dir_attempt_id": self.task_artifact_dir_attempt_id,
            "task_level_reason_code": self.task_level_reason_code,
        }


def _read_only_uri(db_path: Path) -> str:
    """Return a read-only sqlite URI for ``db_path``, whatever its characters.

    ``Path.as_uri`` percent-encodes ``?``, ``#`` and ``%``, which a raw
    ``file:{path}`` URI would read as the query, the fragment or an escape.
    """
    return f"{Path(db_path).absolute().as_uri()}?mode=ro"


def _task_level_directory(
    db_path: Path, task_key: str, task_dir: Path | None
) -> tuple[Path | None, str | None, str | None, str | None]:
    """Return the task-level directory, whose root it was, and why.

    The last two values are the task-level reason code and its reason. Read
    only: a database without ``attempt_resources`` records no Attempt roots,
    so ``task_dir`` is the task level. A lookup that fails keeps ``task_dir``
    but says so, because that directory may be an Attempt's root.
    """
    if task_dir is None:
        return None, None, None, None
    try:
        with closing(sqlite3.connect(_read_only_uri(db_path), uri=True)) as conn:
            has_resources = conn.execute(
                "SELECT 1 FROM sqlite_master"
                " WHERE type = 'table' AND name = 'attempt_resources'"
            ).fetchone()
            row = None
            if has_resources is not None:
                row = conn.execute(
                    "SELECT attempt_id, artifact_base_root FROM attempt_resources"
                    " WHERE task_key = ? AND artifact_root = ?"
                    " ORDER BY attempt_number DESC LIMIT 1",
                    (task_key, str(task_dir)),
                ).fetchone()
    except sqlite3.Error as exc:
        return (
            task_dir,
            None,
            TASK_LEVEL_UNRESOLVED,
            (
                f"Whether {task_dir} is an Attempt's root could not be read "
                f"({type(exc).__name__}: {exc}), so it is used as recorded and "
                "may be the newest Attempt's root."
            ),
        )
    if row is None or not row[1]:
        return task_dir, None, TASK_LEVEL_RECORDED, None
    return Path(row[1]), str(row[0]), TASK_LEVEL_FROM_ATTEMPT_BASE, None


def resolve_integration_evidence_root(
    db_path: Path,
    *,
    task_key: str,
    task_artifact_dir: Path | None,
    producer_binding: ProducerAttemptBinding | None,
) -> IntegrationEvidenceRoot:
    """Return the producer Attempt's root, or the task level with the reason."""
    task_dir = None if task_artifact_dir is None else Path(task_artifact_dir)

    def task_level(
        reason_code: str, reason: str, attempt_id: str | None = None
    ) -> IntegrationEvidenceRoot:
        level, owner, level_code, level_reason = _task_level_directory(
            db_path, task_key, task_dir
        )
        if owner is not None:
            reason += (
                f" The task's recorded artifact directory is Attempt {owner}'s "
                f"root, so the evidence is written at its task level {level} "
                "instead of being filed under that Attempt."
            )
        if level_reason is not None:
            reason += f" {level_reason}"
        return IntegrationEvidenceRoot(
            path=level,
            scope=SCOPE_TASK if level is not None else SCOPE_NONE,
            reason_code=reason_code,
            reason=reason,
            attempt_id=attempt_id,
            task_artifact_dir=task_dir,
            task_artifact_dir_attempt_id=owner,
            task_level_reason_code=level_code,
        )

    if producer_binding is None or not producer_binding.bound:
        detail = (
            "no producer binding was resolved"
            if producer_binding is None
            else f"the producer binding is {producer_binding.reason_code}"
        )
        return task_level(
            REASON_NO_PRODUCER,
            f"This run has no authoritative producer Attempt ({detail}), so its "
            "evidence stays at the task level.",
        )

    attempt_id = str(producer_binding.attempt_id)
    try:
        attempt = AttemptStore(db_path).get_attempt(attempt_id)
    except Exception as exc:  # noqa: BLE001 - a store without the Attempt tables.
        return task_level(
            REASON_ATTEMPT_UNREADABLE,
            f"Producer Attempt {attempt_id} could not be read "
            f"({type(exc).__name__}: {exc}).",
            attempt_id,
        )
    root = None if attempt is None else attempt.artifact_root
    if root is None:
        return task_level(
            REASON_NO_ATTEMPT_ROOT,
            f"Producer Attempt {attempt_id} records no artifact root; it was not "
            "allocated Attempt-scoped resources.",
            attempt_id,
        )
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        return task_level(
            REASON_ATTEMPT_ROOT_UNSAFE,
            f"Producer Attempt {attempt_id} records an unsafe artifact root {root}.",
            attempt_id,
        )
    if root.is_symlink() or not root.is_dir():
        return task_level(
            REASON_ATTEMPT_ROOT_UNAVAILABLE,
            f"Producer Attempt {attempt_id}'s artifact root {root} is not an "
            "existing directory; it is reported, not recreated.",
            attempt_id,
        )
    return IntegrationEvidenceRoot(
        path=root,
        scope=SCOPE_ATTEMPT,
        reason_code=REASON_ATTEMPT_ROOT,
        reason=(
            f"Written under the artifact root of producer Attempt {attempt_id}, "
            "bound through this run's own queue entry."
        ),
        attempt_id=attempt_id,
        task_artifact_dir=task_dir,
    )
