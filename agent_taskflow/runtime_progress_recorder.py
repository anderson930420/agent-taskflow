"""Best-effort runtime progress writes from the execution loop (FOLLOWUPS F1).

Step 3 owns the write surface, :class:`RuntimeProgressStore`. This module is its
producer: the dispatcher and the approved-task runner report the §14.1 Prepare,
Implementer and Validator transitions, plus §14 ``current_phase`` and
``current_activity``, for the Attempt their runtime-admission claim reserved.

Progress is observation, not lifecycle. Every write is best-effort: a failure is
logged and swallowed, so it can never change a run's outcome. The recorder never
migrates, so a database without Step 3's tables records nothing.

Text is plain and factual (§14.2): step names, executor and validator names, and
result statuses. Executor and validator summaries are never copied in; they are
free text and may state a percentage or an estimate.

Scout, Planner and Reviewer are not written. No executor exposes those phases
deterministically: the Pi mission plan names them, but they all run inside one
executor invocation. Integration belongs to Step 2.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from agent_taskflow.runtime_progress import (
    RUNTIME_STEP_STATUS_FAILED,
    RUNTIME_STEP_STATUS_PASSED,
    RUNTIME_STEP_STATUS_RUNNING,
)
from agent_taskflow.runtime_progress_store import RuntimeProgressStore


logger = logging.getLogger(__name__)

STEP_PREPARE = "Prepare"
STEP_IMPLEMENTER = "Implementer"
STEP_VALIDATOR = "Validator"


def claimed_attempt_id(task_store: Any, task_key: str) -> str | None:
    """Return the Attempt id of ``task_store``'s live runtime claim, if any.

    Only a live claim counts: a released one can belong to an earlier run on
    the same store. A store without the claim API yields ``None``.
    """

    runtime_claim = getattr(task_store, "runtime_claim", None)
    if runtime_claim is None:
        return None
    try:
        claim = runtime_claim(task_key)
    except Exception:  # noqa: BLE001 - identification is best-effort as well.
        return None
    return getattr(claim, "attempt_id", None) if claim is not None else None


def _names(names: Sequence[str]) -> str:
    return ", ".join(names)


class RuntimeProgressRecorder:
    """Record one Attempt's runtime progress. No method ever raises."""

    def __init__(
        self,
        attempt_id: str | None,
        *,
        source: str,
        store: Any | None = None,
    ) -> None:
        self.attempt_id = attempt_id
        self.source = source
        self._store = store if attempt_id is not None else None
        self._warned = False

    @classmethod
    def for_claim(
        cls,
        task_store: Any,
        task_key: str,
        *,
        source: str,
        progress_store: Any | None = None,
        previous_attempt_id: str | None = None,
    ) -> "RuntimeProgressRecorder":
        """Bind to the Attempt of ``task_store``'s live claim on ``task_key``.

        ``previous_attempt_id`` is the claim that was live before the caller
        tried to claim; a recorder is never bound to it, so a refused claim
        cannot write onto another run's Attempt.
        """

        attempt_id = claimed_attempt_id(task_store, task_key)
        if attempt_id is None or attempt_id == previous_attempt_id:
            return cls(None, source=source)
        store = progress_store
        if store is None:
            try:
                store = RuntimeProgressStore(task_store.db_path)
            except Exception as exc:  # noqa: BLE001 - progress is not lifecycle.
                logger.warning(
                    "Runtime progress disabled for attempt %s: %s: %s",
                    attempt_id,
                    exc.__class__.__name__,
                    exc,
                )
                store = None
        return cls(attempt_id, source=source, store=store)

    # -- generic writes ------------------------------------------------------

    def step(
        self,
        step: str,
        status: str,
        activity: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a step transition and make it the current phase and activity."""

        self._write(
            "record_step",
            step=step,
            status=status,
            summary=activity,
            metadata={"source": self.source, **dict(metadata or {})},
        )
        self.activity(step, activity)

    def activity(self, step: str, activity: str) -> None:
        """Update ``current_phase`` / ``current_activity`` only."""

        self._write("set_current_activity", phase=step, activity=activity)

    def _write(self, method: str, **kwargs: Any) -> None:
        if self._store is None:
            return
        try:
            getattr(self._store, method)(attempt_id=self.attempt_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - progress is not lifecycle.
            # Keep trying later writes (a failure may be transient), but warn
            # once per run: a database without Step 3's tables fails every
            # write the same way.
            logger.log(
                logging.DEBUG if self._warned else logging.WARNING,
                "Runtime progress write %s failed for attempt %s: %s: %s",
                method,
                self.attempt_id,
                exc.__class__.__name__,
                exc,
            )
            self._warned = True

    # -- Prepare -------------------------------------------------------------

    def prepare_running(self, executor: str) -> None:
        self.step(
            STEP_PREPARE,
            RUNTIME_STEP_STATUS_RUNNING,
            f"Task claimed; preparing executor {executor}",
            metadata={"executor": executor},
        )

    def prepare_passed(self, executor: str) -> None:
        self.step(
            STEP_PREPARE,
            RUNTIME_STEP_STATUS_PASSED,
            f"Executor {executor} context prepared",
            metadata={"executor": executor},
        )

    def prepare_failed(self, activity: str) -> None:
        self.step(STEP_PREPARE, RUNTIME_STEP_STATUS_FAILED, activity)

    # -- Implementer ---------------------------------------------------------

    def implementer_running(self, executor: str) -> None:
        self.step(
            STEP_IMPLEMENTER,
            RUNTIME_STEP_STATUS_RUNNING,
            f"Running executor {executor}",
            metadata={"executor": executor},
        )

    def implementer_finished(self, executor: str, result_status: str, *, passed: bool) -> None:
        self.step(
            STEP_IMPLEMENTER,
            RUNTIME_STEP_STATUS_PASSED if passed else RUNTIME_STEP_STATUS_FAILED,
            f"Executor {executor} returned {result_status}",
            metadata={"executor": executor, "executor_status": result_status},
        )

    def implementer_raised(self, executor: str, exc: BaseException) -> None:
        self.step(
            STEP_IMPLEMENTER,
            RUNTIME_STEP_STATUS_FAILED,
            f"Executor {executor} raised {exc.__class__.__name__}",
            metadata={"executor": executor},
        )

    # -- Validator -----------------------------------------------------------

    def validators_running(self, validators: Sequence[str]) -> None:
        self.step(
            STEP_VALIDATOR,
            RUNTIME_STEP_STATUS_RUNNING,
            f"Running validators: {_names(validators)}"
            if validators
            else "No validators configured",
            metadata={"validators": list(validators)},
        )

    def validator_running(self, validator: str) -> None:
        self.activity(STEP_VALIDATOR, f"Running validator {validator}")

    def validator_failed(self, validator: str, result_status: str) -> None:
        self.step(
            STEP_VALIDATOR,
            RUNTIME_STEP_STATUS_FAILED,
            f"Validator {validator} returned {result_status}",
            metadata={"validator": validator, "validator_status": result_status},
        )

    def validator_raised(self, validator: str, exc: BaseException) -> None:
        self.step(
            STEP_VALIDATOR,
            RUNTIME_STEP_STATUS_FAILED,
            f"Validator {validator} raised {exc.__class__.__name__}",
            metadata={"validator": validator},
        )

    def validators_passed(self, validator_statuses: Mapping[str, str]) -> None:
        results = [f"{name} {status}" for name, status in validator_statuses.items()]
        self.step(
            STEP_VALIDATOR,
            RUNTIME_STEP_STATUS_PASSED,
            f"Validators finished: {_names(results)}"
            if results
            else "Validation finished with no validators configured",
            metadata={"validator_statuses": dict(validator_statuses)},
        )


__all__ = [
    "STEP_IMPLEMENTER",
    "STEP_PREPARE",
    "STEP_VALIDATOR",
    "RuntimeProgressRecorder",
    "claimed_attempt_id",
]
