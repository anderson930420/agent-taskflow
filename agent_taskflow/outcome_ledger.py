"""Immutable per-Attempt outcome ledger for Level 2 Roadmap M2 §2.3.

Every terminal Attempt route publishes exactly one immutable closeout snapshot,
or an explicit attributable evidence error. The writer runs *after* the terminal
lifecycle transaction has committed, so a publication failure can never roll
back, retry, or reinterpret a lifecycle result.

Binding rules this module enforces:

* The exact Attempt id supplied by the terminal route is read. There is no
  latest-Attempt or newest-row inference anywhere in this module.
* Retry history is derived only from Attempts whose ``attempt_number`` is less
  than or equal to this Attempt, so replaying an older Attempt's ledger after a
  later retry produces the same counts.
* The Attempt's own final status is recorded separately from the Task status
  observed at closeout; they are different facts.
* Facts that have not happened yet at closeout -- the human merge decision, the
  post-merge result and the revert/rollback result -- are ``null`` with
  ``unknown`` provenance. They are never guessed and never defaulted to zero.
* A later, actually observed outcome is appended as its own uniquely identified
  enrichment artifact that references the base ledger. The base ledger is
  published once and is never rewritten.

Publication reuses the anchored, symlink-refusing, publish-once helpers that
managed-launch evidence already uses (``launch_evidence``), and the existing
``task_artifacts`` / ``task_events`` indexes. No schema migration is required
and no new mutation endpoint is added.

Nothing in this module is a lifecycle, merge or approval authority.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4

from agent_taskflow.attempt_failure_class import (
    FAILURE_CLASS_KEY,
    FAILURE_CLASS_REASON_KEY,
    FAILURE_KIND_KEY,
    RECORDED_FAILURE_CLASSES,
)
from agent_taskflow.launch_evidence import (
    _open_directory_without_symlinks as open_directory_without_symlinks,
    _publish_once as publish_once,
)
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore, connect

__all__ = [
    "CLOSEOUT_ROUTES",
    "ENRICHABLE_FIELDS",
    "LEDGER_ARTIFACT_TYPE",
    "LEDGER_EVENT_TYPE",
    "LEDGER_FIELDS",
    "LEDGER_SCHEMA_VERSION",
    "OBSERVATION_SCHEMA_VERSION",
    "ledger_filename",
    "record_attempt_outcome_observation",
    "record_terminal_attempt_outcome",
    "record_trigger_closeout_outcome",
]

logger = logging.getLogger(__name__)

LEDGER_SCHEMA_VERSION = "attempt_outcome_ledger.v1"
OBSERVATION_SCHEMA_VERSION = "attempt_outcome_observation.v1"
LEDGER_RECORD_TYPE = "base_closeout_snapshot"
OBSERVATION_RECORD_TYPE = "later_observation"

# Existing vocabulary only: `other` artifacts and `note` events already index
# runner evidence such as validation summaries, so no migration is needed.
LEDGER_ARTIFACT_TYPE = "other"
LEDGER_EVENT_TYPE = "note"
LEDGER_EVENT_SOURCE = "outcome_ledger"

MAX_EVIDENCE_BYTES = 1_000_000

#: The terminal writer routes. `LifecycleRuntimeTaskStore._release` is
#: deliberately absent: it delegates to `RuntimeAdmissionStore.release`, and a
#: second hook there would double-write the same Attempt.
CLOSEOUT_ROUTES = frozenset(
    {
        "runtime_admission_release",
        "runtime_lease_expiry",
        "attempt_store_close",
        "task_status_trigger",
    }
)

LEDGER_FIELDS = (
    "final_status",
    "failure_class",
    "phase_durations",
    "retry_count",
    "first_pass_success",
    "human_intervention_count",
    "diff_size",
    "task_class",
    "policy_version",
    "model_snapshot",
    "canonical_execution_path",
    "merge_recommendation",
    "human_decision",
    "post_merge_result",
    "rollback_result",
)

#: Only these fields may be added by a later observation. They are exactly the
#: fields that cannot exist yet when an Attempt terminalizes.
ENRICHABLE_FIELDS = ("human_decision", "post_merge_result", "rollback_result")

#: An Attempt reached a successful terminal state. `waiting_approval` is the
#: Attempt-level success terminal for both the legacy approval path and the V1
#: `ready_for_integration` path (see `lifecycle_runtime_path._release`).
SUCCESS_ATTEMPT_STATUSES = frozenset({"completed", "waiting_approval"})

#: Terminal Attempt statuses that are not failures, so carry no failure class.
#: `canceled` is an operator decision.
NON_FAILURE_ATTEMPT_STATUSES = SUCCESS_ATTEMPT_STATUSES | {"canceled"}

#: Explicit, attributable, Attempt-bound human interventions. Counting is by
#: documented reason code on this Attempt's own lifecycle events only. Actor
#: names are never pattern-matched, and task-scoped events that carry no
#: Attempt binding are never attributed to an Attempt.
HUMAN_INTERVENTION_REASON_CODES = (
    "operator_kill_requested",
    "operator_pause_requested",
    "operator_task_class_governance_disabled",
    "reset_retry_attempt_reserved",
    "reset_retry_attempt_claimed",
)

#: Directly recorded producer proof: the reason code of the Attempt's own
#: creation/claim lifecycle event. An unrecognized code stays `unknown`.
CANONICAL_EXECUTION_PATH_BY_PRODUCER = {
    "canonical_runtime_pickup_claimed": "canonical_runtime_path",
    "runtime_pickup_claimed": "runtime_admission_claim",
    "runtime_pickup_claimed_implicit": "compatibility_implicit_claim",
    "reset_retry_attempt_claimed": "reset_retry_claim",
    "reset_retry_attempt_reserved": "reset_retry_reservation",
    "attempt_created": "attempt_store_direct",
}

CHANGED_FILES_AUDIT_NAME = "changed-files-audit.json"
TRIGGER_CLOSEOUT_REASON_CODE = "runtime_attempt_closed_by_task_status"

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


# --------------------------------------------------------------------------
# paths and provenance primitives
# --------------------------------------------------------------------------


def ledger_filename(attempt_id: str) -> str:
    """Return the deterministic idempotency key for one Attempt's ledger.

    Attempt ids may be caller-supplied through ``AttemptStore.create_attempt``,
    so an id that is not a single safe path component is hashed rather than
    used verbatim. The mapping is deterministic, so repeated publication of the
    same Attempt still collides on the same name.
    """
    if _SAFE_ID.fullmatch(attempt_id) and attempt_id not in {".", ".."}:
        return f"outcome-ledger-{attempt_id}.json"
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    return f"outcome-ledger-sha256-{digest}.json"


def _observation_filename(attempt_id: str, observation_id: str) -> str:
    base = ledger_filename(attempt_id)[: -len(".json")]
    return f"{base}-observation-{observation_id}.json"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Mirror the byte form `launch_evidence._publish_once` publishes."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _field(
    value: Any,
    provenance: str,
    *,
    source: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "value": value,
        "provenance": provenance,
        "source": source,
        "reason": reason,
    }
    record.update(extra)
    return record


def _unknown(reason: str, **extra: Any) -> dict[str, Any]:
    return _field(None, "unknown", reason=reason, **extra)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    first = _parse_timestamp(start)
    second = _parse_timestamp(end)
    if first is None or second is None:
        return None
    return round((second - first).total_seconds(), 6)


def _ensure_root(root: Path) -> None:
    """Materialize exactly the Attempt's own recorded artifact root.

    Walks the recorded absolute path one component at a time, never following a
    symlink: an existing symlinked component makes ``O_NOFOLLOW`` fail, so a
    substituted parent is refused instead of silently redirecting the ledger.
    This creates only the recorded root; it never invents a fallback location.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def _read_json_under_root(
    root: Path, name: str, *, max_bytes: int = MAX_EVIDENCE_BYTES
) -> tuple[Any, str | None]:
    """Read one regular JSON file directly beneath ``root`` without symlinks."""
    if not name or Path(name).name != name or name in {".", ".."}:
        return None, "invalid_evidence_name"
    directory = -1
    try:
        directory = open_directory_without_symlinks(root)
    except ValueError:
        return None, "unsafe_artifact_root"
    except OSError as exc:
        return None, f"artifact_root_unavailable:{type(exc).__name__}:{exc.errno}"
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None, "not_a_regular_file"
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        return None, f"unreadable:{type(exc).__name__}:{exc.errno}"
    finally:
        os.close(directory)
    if len(raw) > max_bytes:
        return None, "evidence_too_large"
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"corrupt_evidence:{type(exc).__name__}"


# --------------------------------------------------------------------------
# read-only Attempt snapshot
# --------------------------------------------------------------------------


def _row_value(row: Any, column: str) -> Any:
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def _read_snapshot(db_path: Path, attempt_id: str) -> dict[str, Any]:
    """Read the exact Attempt plus its own task, retry history and events.

    One read transaction, all queries keyed by this Attempt's own identity.
    Retry history is bounded to ``attempt_number <= this Attempt`` and lifecycle
    events are bounded to this ``attempt_id``; there is no global scan.
    """
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN")
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            return {"attempt": None, "task": None, "history": [], "events": []}
        task = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (attempt["task_id"],)
        ).fetchone()
        history = conn.execute(
            """
            SELECT attempt_id, attempt_number, status, is_active, ended_at
            FROM attempts
            WHERE task_id = ? AND attempt_number <= ?
            ORDER BY attempt_number ASC
            """,
            (attempt["task_id"], attempt["attempt_number"]),
        ).fetchall()
        events = conn.execute(
            """
            SELECT event_id, attempt_id, from_status, to_status, reason_code,
                   actor, timestamp, metadata_json
            FROM lifecycle_events
            WHERE attempt_id = ?
            ORDER BY event_id ASC
            """,
            (attempt_id,),
        ).fetchall()
    return {
        "attempt": attempt,
        "task": task,
        "history": history,
        "events": events,
    }


# --------------------------------------------------------------------------
# field derivation
# --------------------------------------------------------------------------


def _phase_durations_field(
    events: list[Any], attempt_id: str, ended_at: str | None
) -> dict[str, Any]:
    if not events:
        return _unknown("no_attempt_bound_lifecycle_events")
    segments: list[dict[str, Any]] = []
    for event in events:
        if segments and segments[-1]["phase"] == event["to_status"]:
            # Same phase (for example a heartbeat); not a new phase boundary.
            continue
        segments.append(
            {
                "phase": event["to_status"],
                "started_at": event["timestamp"],
                "start_event_id": event["event_id"],
                "ended_at": None,
                "end_event_id": None,
                "duration_seconds": None,
            }
        )
    for index in range(len(segments) - 1):
        following = segments[index + 1]
        segments[index]["ended_at"] = following["started_at"]
        segments[index]["end_event_id"] = following["start_event_id"]
    last = segments[-1]
    last["ended_at"] = ended_at
    last["end_source"] = f"attempts.ended_at@{attempt_id}"
    for segment in segments:
        segment["duration_seconds"] = _duration_seconds(
            segment["started_at"], segment["ended_at"]
        )
    incomplete = [
        segment["phase"]
        for segment in segments
        if segment["duration_seconds"] is None
    ]
    return _field(
        segments,
        "observed",
        source=f"lifecycle_events(attempt_id={attempt_id}) + attempts.ended_at",
        reason=None if not incomplete else "unparsable_or_missing_phase_boundary",
        incomplete_phases=incomplete,
        semantics=(
            "Consecutive same-Attempt status transitions. Repeated events inside "
            "one status do not open a new phase. The terminal phase ends at this "
            "Attempt's own ended_at."
        ),
    )


def _diff_size_field(artifact_root: Path | None, attempt_id: str) -> dict[str, Any]:
    if artifact_root is None:
        return _unknown("attempt_artifact_root_missing")
    audit, problem = _read_json_under_root(artifact_root, CHANGED_FILES_AUDIT_NAME)
    source = str(artifact_root / CHANGED_FILES_AUDIT_NAME)
    if problem is not None:
        return _unknown(f"changed_files_audit_{problem}", source=source)
    if not isinstance(audit, dict) or not isinstance(audit.get("changed_files"), list):
        return _unknown("changed_files_audit_unexpected_shape", source=source)
    changed = audit["changed_files"]
    return _field(
        {
            "changed_file_count": len(changed),
            "violation_count": len(audit.get("violations") or []),
            "collection_error": audit.get("collection_error"),
        },
        "observed",
        source=source,
        semantics=(
            "Changed-file count from the changed-files validator audit written "
            f"into this Attempt's own artifact root ({attempt_id}). Line counts "
            "are not recorded by that validator and are not inferred here."
        ),
    )


def _canonical_execution_path_field(events: list[Any]) -> dict[str, Any]:
    if not events:
        return _unknown("no_attempt_bound_lifecycle_events")
    producer = events[0]
    reason_code = producer["reason_code"]
    source = f"lifecycle_events.event_id={producer['event_id']}"
    mapped = CANONICAL_EXECUTION_PATH_BY_PRODUCER.get(reason_code)
    if mapped is None:
        return _unknown(
            "unrecognized_producer_reason_code",
            source=source,
            producer_reason_code=reason_code,
            producer_actor=producer["actor"],
        )
    return _field(
        mapped,
        "observed",
        source=source,
        producer_reason_code=reason_code,
        producer_actor=producer["actor"],
        semantics="Derived from this Attempt's own recorded creation/claim event.",
    )


def _human_intervention_field(events: list[Any], attempt_id: str) -> dict[str, Any]:
    matched = [
        event
        for event in events
        if event["reason_code"] in HUMAN_INTERVENTION_REASON_CODES
    ]
    return _field(
        len(matched),
        "observed",
        source=f"lifecycle_events(attempt_id={attempt_id})",
        counted_reason_codes=list(HUMAN_INTERVENTION_REASON_CODES),
        matched_event_ids=[event["event_id"] for event in matched],
        semantics=(
            "Explicit, Attempt-bound, documented operator interventions only. "
            "Task-scoped approval or review events carry no Attempt binding and "
            "are deliberately not counted here."
        ),
    )


def _first_pass_success_field(history: list[Any]) -> dict[str, Any]:
    first = next((row for row in history if row["attempt_number"] == 1), None)
    if first is None:
        return _unknown("first_attempt_row_not_found")
    if first["is_active"] or first["ended_at"] is None:
        return _unknown(
            "first_attempt_not_terminal", source=f"attempts.attempt_id={first['attempt_id']}"
        )
    return _field(
        first["status"] in SUCCESS_ATTEMPT_STATUSES,
        "observed",
        source=f"attempts.attempt_id={first['attempt_id']}",
        first_attempt_id=first["attempt_id"],
        first_attempt_status=first["status"],
        semantics=(
            "True only when Attempt 1 of this Task itself reached a successful "
            f"terminal Attempt status ({sorted(SUCCESS_ATTEMPT_STATUSES)})."
        ),
    )


def _failure_class_field(attempt: Any, events: list[Any]) -> dict[str, Any]:
    """M2 Exit Gate row 2: the class recorded on this Attempt's terminal event.

    Read, never derived from statuses: an Attempt that terminalized without a
    recorded class (a lease expiry, a direct close, or one closed before the
    class existed) is ``unknown``, not guessed.
    """
    attempt_id = attempt["attempt_id"]
    if attempt["status"] in NON_FAILURE_ATTEMPT_STATUSES:
        return _field(
            None,
            "not_applicable",
            source=f"attempts.status@{attempt_id}",
            reason="attempt_did_not_fail",
        )
    if not events:
        return _unknown("no_attempt_bound_lifecycle_events")
    terminal = events[-1]
    source = f"lifecycle_events.event_id={terminal['event_id']}.metadata_json"
    try:
        metadata = json.loads(_row_value(terminal, "metadata_json") or "{}")
    except ValueError:
        return _unknown("terminal_event_metadata_unreadable", source=source)
    if not isinstance(metadata, dict) or FAILURE_CLASS_KEY not in metadata:
        return _unknown("failure_class_not_recorded_on_terminal_event", source=source)
    value = metadata[FAILURE_CLASS_KEY]
    if value not in RECORDED_FAILURE_CLASSES:
        return _unknown(
            "unrecognized_failure_class", source=source, recorded_value=value
        )
    return _field(
        value,
        "observed",
        source=source,
        reason=metadata.get(FAILURE_CLASS_REASON_KEY),
        failure_kind=metadata.get(FAILURE_KIND_KEY),
        semantics=(
            "execution_failure, validation_failure or tool_error, mapped from the "
            "runner's failure kind when the Attempt terminalized; `unknown` when "
            "the kind was missing or unmapped. Not a status."
        ),
    )


def _build_fields(snapshot: dict[str, Any], artifact_root: Path | None) -> dict[str, Any]:
    attempt = snapshot["attempt"]
    task = snapshot["task"]
    history = snapshot["history"]
    events = snapshot["events"]
    attempt_id = attempt["attempt_id"]
    preceding = [row for row in history if row["attempt_number"] < attempt["attempt_number"]]

    task_class = _row_value(task, "task_class") if task is not None else None
    fields: dict[str, Any] = {
        "final_status": _field(
            attempt["status"],
            "observed",
            source=f"attempts.status@{attempt_id}",
            execution_result=attempt["execution_result"],
            validation_result=attempt["validation_result"],
            semantics="The Attempt's own final status, not the Task status.",
        ),
        "failure_class": _failure_class_field(attempt, events),
        "phase_durations": _phase_durations_field(events, attempt_id, attempt["ended_at"]),
        "retry_count": _field(
            len(preceding),
            "observed",
            source=(
                f"attempts(task_id={attempt['task_id']}, "
                f"attempt_number <= {attempt['attempt_number']})"
            ),
            preceding_attempt_ids=[row["attempt_id"] for row in preceding],
            semantics=(
                "Attempts preceding this Attempt only. Later retries cannot "
                "change this Attempt's replayed ledger."
            ),
        ),
        "first_pass_success": _first_pass_success_field(history),
        "human_intervention_count": _human_intervention_field(events, attempt_id),
        "diff_size": _diff_size_field(artifact_root, attempt_id),
        "task_class": (
            _field(task_class, "observed", source=f"tasks.task_class@{attempt['task_id']}")
            if task_class
            else _unknown("task_class_not_recorded")
        ),
        "policy_version": (
            _field(
                attempt["policy_version"],
                "observed",
                source=f"attempts.policy_version@{attempt_id}",
            )
            if attempt["policy_version"]
            else _unknown("not_recorded_on_attempt")
        ),
        "model_snapshot": _field(
            {
                "configured_model": attempt["model"],
                "executor": attempt["executor"],
                "prompt_template_version": attempt["prompt_template_version"],
                "config_snapshot_hash": attempt["config_snapshot_hash"],
                "base_commit": attempt["base_commit"],
                "permission_profile": attempt["permission_profile"],
                "observed_backend_model": None,
            },
            "observed" if attempt["model"] else "unknown",
            source=f"attempts@{attempt_id}",
            reason=None if attempt["model"] else "configured_model_not_recorded",
            observed_backend_model_provenance="unknown",
            observed_backend_model_reason="runtime_backend_model_not_attested_at_this_boundary",
            semantics=(
                "Requested/configured Attempt configuration. The model a backend "
                "actually served is a different fact and is not attested here."
            ),
        ),
        "canonical_execution_path": _canonical_execution_path_field(events),
        "merge_recommendation": (
            _field(
                attempt["merge_recommendation"],
                "observed",
                source=f"attempts.merge_recommendation@{attempt_id}",
            )
            if attempt["merge_recommendation"]
            else _unknown("not_recorded_on_attempt")
        ),
    }
    for name in ENRICHABLE_FIELDS:
        fields[name] = _unknown(
            "not_observed_at_attempt_closeout",
            append_only_enrichment=True,
            enrichment_record_type=OBSERVATION_RECORD_TYPE,
        )
    return fields


def _build_payload(
    snapshot: dict[str, Any],
    *,
    closeout_route: str,
    artifact_root: Path | None,
    ledger_name: str,
) -> dict[str, Any]:
    attempt = snapshot["attempt"]
    task = snapshot["task"]
    events = snapshot["events"]
    terminal_event = events[-1] if events else None
    task_key = _row_value(task, "task_key") if task is not None else None
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "kind": "attempt_outcome_ledger",
        "record_type": LEDGER_RECORD_TYPE,
        "immutable": True,
        "lifecycle_authority": False,
        "merge_authority": False,
        "task_id": attempt["task_id"],
        "task_key": task_key,
        "attempt_id": attempt["attempt_id"],
        "attempt_number": attempt["attempt_number"],
        "attempt_id_filename_safe": ledger_name == f"outcome-ledger-{attempt['attempt_id']}.json",
        "artifact_name": ledger_name,
        "closeout_route": closeout_route,
        "closeout_route_semantics": (
            "The writer callback that published this ledger. The authoritative "
            "record of how the Attempt terminalized is terminal_lifecycle_event."
        ),
        "recorded_at": utc_now_iso(),
        "attempt_terminal": {
            "status": attempt["status"],
            "is_active": bool(attempt["is_active"]),
            "started_at": attempt["started_at"],
            "ended_at": attempt["ended_at"],
            "execution_result": attempt["execution_result"],
            "validation_result": attempt["validation_result"],
            "total_duration_seconds": _duration_seconds(
                attempt["started_at"], attempt["ended_at"]
            ),
            "worktree_path": attempt["worktree_path"],
            "artifact_root": attempt["artifact_root"],
        },
        "task_status_at_closeout": (
            _field(
                _row_value(task, "status"),
                "observed",
                source=f"tasks.status@{attempt['task_id']}",
                semantics="Task status observed after the terminal commit; not the Attempt outcome.",
            )
            if task is not None
            else _unknown("task_row_not_found")
        ),
        "terminal_lifecycle_event": (
            {
                "event_id": terminal_event["event_id"],
                "from_status": terminal_event["from_status"],
                "to_status": terminal_event["to_status"],
                "reason_code": terminal_event["reason_code"],
                "actor": terminal_event["actor"],
                "timestamp": terminal_event["timestamp"],
            }
            if terminal_event is not None
            else None
        ),
        "attempt_bound_lifecycle_event_ids": [event["event_id"] for event in events],
        "fields": _build_fields(snapshot, artifact_root),
        "later_observations": {
            "append_only": True,
            "record_type": OBSERVATION_RECORD_TYPE,
            "enrichable_fields": list(ENRICHABLE_FIELDS),
            "note": (
                "Later observations are separate artifacts referencing this "
                "ledger by digest. This snapshot is never rewritten."
            ),
        },
    }


def _identity(payload: dict[str, Any]) -> tuple[Any, ...]:
    """Immutable facts that two publications of one Attempt must agree on."""
    fields = payload.get("fields") or {}
    final_status = fields.get("final_status") or {}
    terminal = payload.get("attempt_terminal") or {}
    return (
        payload.get("schema_version"),
        payload.get("record_type"),
        payload.get("task_id"),
        payload.get("attempt_id"),
        payload.get("attempt_number"),
        final_status.get("value"),
        terminal.get("ended_at"),
    )


# --------------------------------------------------------------------------
# indexing and error observations
# --------------------------------------------------------------------------


def _reference(
    attempt_id: str,
    *,
    task_key: str | None,
    closeout_route: str,
    status: str,
    path: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    reference: dict[str, Any] = {
        "kind": "attempt_outcome_ledger_reference",
        "schema_version": LEDGER_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "task_key": task_key,
        "closeout_route": closeout_route,
        "status": status,
        "path": path,
        "reason": reason,
        "recorded_at": utc_now_iso(),
        "lifecycle_authority": False,
    }
    reference.update(extra)
    return reference


def _record_event(
    db_path: Path,
    task_key: str | None,
    reference: dict[str, Any],
    *,
    message: str,
) -> None:
    """Index the outcome, including an explicit incomplete-evidence outcome.

    A failure here is logged and never raised: the caller's lifecycle result has
    already been committed and must not change because evidence indexing failed.
    """
    if not task_key:
        logger.warning(
            "Outcome ledger observation has no task binding to index: %s", reference
        )
        return
    try:
        TaskMirrorStore(db_path).record_task_event(
            task_key,
            LEDGER_EVENT_TYPE,
            LEDGER_EVENT_SOURCE,
            message=message,
            payload=reference,
        )
    except Exception as exc:  # pragma: no cover - defensive audit path
        logger.warning("Outcome ledger event indexing failed: %s", exc)


def _record_artifact(db_path: Path, task_key: str | None, path: Path) -> str | None:
    if not task_key:
        return "no_task_binding_for_artifact_index"
    try:
        TaskMirrorStore(db_path).record_task_artifact(task_key, LEDGER_ARTIFACT_TYPE, path)
    except Exception as exc:
        logger.warning("Outcome ledger artifact indexing failed: %s", exc)
        return f"artifact_index_failed:{type(exc).__name__}"
    return None


# --------------------------------------------------------------------------
# public writers
# --------------------------------------------------------------------------


def record_terminal_attempt_outcome(
    *,
    db_path: str | Path | None,
    attempt_id: str,
    closeout_route: str,
    task_key_hint: str | None = None,
    expected_task_id: str | None = None,
    expected_task_key: str | None = None,
) -> dict[str, Any]:
    """Publish the immutable closeout snapshot for one exact terminal Attempt.

    Call this only after the terminal lifecycle transaction has committed. The
    function never raises into its caller and never mutates lifecycle state: an
    unresolvable Attempt, a missing artifact root, an unsafe path, a corrupt
    source or a failed publication all produce an explicit, attributable
    incomplete-evidence observation instead.
    """
    if closeout_route not in CLOSEOUT_ROUTES:
        raise ValueError(f"Unknown outcome ledger closeout route: {closeout_route!r}")
    resolved_db = Path(db_path) if db_path is not None else None
    try:
        return _record_terminal_attempt_outcome(
            db_path=resolved_db,
            attempt_id=attempt_id,
            closeout_route=closeout_route,
            task_key_hint=task_key_hint,
            expected_task_id=expected_task_id,
            expected_task_key=expected_task_key,
        )
    except Exception as exc:  # pragma: no cover - the writer must never escalate
        logger.warning("Outcome ledger writer failed for %s: %s", attempt_id, exc)
        return _reference(
            attempt_id,
            task_key=task_key_hint,
            closeout_route=closeout_route,
            status="incomplete",
            reason="outcome_ledger_writer_error",
            error={"type": type(exc).__name__, "message": str(exc)},
        )


def _record_terminal_attempt_outcome(
    *,
    db_path: Path | None,
    attempt_id: str,
    closeout_route: str,
    task_key_hint: str | None,
    expected_task_id: str | None,
    expected_task_key: str | None,
) -> dict[str, Any]:
    # The task binding starts at the caller's hint and is upgraded to the
    # Attempt's own task as soon as it is read, so an incomplete observation is
    # always indexed against a real task when one can be resolved.
    binding: dict[str, str | None] = {"task_key": task_key_hint}

    def incomplete(reason: str, **extra: Any) -> dict[str, Any]:
        reference = _reference(
            attempt_id,
            task_key=binding["task_key"],
            closeout_route=closeout_route,
            status="incomplete",
            reason=reason,
            **extra,
        )
        _record_event(
            db_path,
            binding["task_key"],
            reference,
            message=f"Outcome ledger evidence incomplete: {reason}",
        )
        return reference

    try:
        snapshot = _read_snapshot(db_path, attempt_id)
    except Exception as exc:
        return incomplete(
            "attempt_snapshot_read_failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )

    attempt = snapshot["attempt"]
    if attempt is None:
        # No fabricated null-identity ledger: the observation is keyed by the
        # task hint and the Attempt id the terminal route actually supplied.
        return incomplete("attempt_row_not_found")

    task = snapshot["task"]
    task_key = _row_value(task, "task_key") if task is not None else task_key_hint
    binding["task_key"] = task_key
    if expected_task_id is not None and attempt["task_id"] != expected_task_id:
        return incomplete(
            "task_identity_mismatch",
            observed_task_id=attempt["task_id"],
            expected_task_id=expected_task_id,
        )
    if expected_task_key is not None and task_key != expected_task_key:
        return incomplete(
            "task_identity_mismatch",
            observed_task_key=task_key,
            expected_task_key=expected_task_key,
        )
    if attempt["is_active"] or attempt["ended_at"] is None:
        return incomplete("attempt_is_not_terminal")

    raw_root = attempt["artifact_root"]
    if not raw_root:
        # No arbitrary fallback root: an Attempt with no recorded artifact root
        # produces an explicit incomplete observation instead.
        return incomplete("attempt_artifact_root_missing")
    artifact_root = Path(raw_root)
    if not artifact_root.is_absolute() or ".." in artifact_root.parts:
        return incomplete("attempt_artifact_root_unsafe")
    try:
        _ensure_root(artifact_root)
    except OSError as exc:
        return incomplete(
            "attempt_artifact_root_unavailable",
            error={"type": type(exc).__name__, "message": str(exc)},
        )

    name = ledger_filename(attempt["attempt_id"])
    payload = _build_payload(
        snapshot,
        closeout_route=closeout_route,
        artifact_root=artifact_root,
        ledger_name=name,
    )
    path = artifact_root / name
    digest = _digest(payload)

    try:
        publish_once(artifact_root, name, payload)
    except FileExistsError:
        return _handle_existing(
            db_path,
            artifact_root=artifact_root,
            name=name,
            path=path,
            payload=payload,
            attempt_id=attempt["attempt_id"],
            task_key=task_key,
            closeout_route=closeout_route,
        )
    except Exception as exc:
        return incomplete(
            "ledger_publication_failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            intended_path=str(path),
        )

    index_error = _record_artifact(db_path, task_key, path)
    reference = _reference(
        attempt["attempt_id"],
        task_key=task_key,
        closeout_route=closeout_route,
        status="published" if index_error is None else "incomplete",
        path=str(path),
        reason=index_error,
        sha256=digest,
        attempt_number=attempt["attempt_number"],
        final_status=attempt["status"],
        artifact_indexed=index_error is None,
    )
    _record_event(
        db_path,
        task_key,
        reference,
        message=(
            "Attempt outcome ledger published"
            if index_error is None
            else f"Outcome ledger published but not indexed: {index_error}"
        ),
    )
    return reference


def _handle_existing(
    db_path: Path | None,
    *,
    artifact_root: Path,
    name: str,
    path: Path,
    payload: dict[str, Any],
    attempt_id: str,
    task_key: str | None,
    closeout_route: str,
) -> dict[str, Any]:
    """A ledger already exists for this Attempt: keep the original, always."""
    existing, problem = _read_json_under_root(artifact_root, name)
    if problem is not None or not isinstance(existing, dict):
        reference = _reference(
            attempt_id,
            task_key=task_key,
            closeout_route=closeout_route,
            status="incomplete",
            path=str(path),
            reason=f"existing_ledger_{problem or 'unexpected_shape'}",
        )
        _record_event(
            db_path,
            task_key,
            reference,
            message="Existing outcome ledger could not be verified; original retained",
        )
        return reference
    if _identity(existing) == _identity(payload):
        reference = _reference(
            attempt_id,
            task_key=task_key,
            closeout_route=closeout_route,
            status="duplicate",
            path=str(path),
            reason="already_published_for_this_attempt",
            sha256=_digest(existing),
            original_closeout_route=existing.get("closeout_route"),
            original_recorded_at=existing.get("recorded_at"),
        )
        _record_event(
            db_path,
            task_key,
            reference,
            message="Attempt outcome ledger already published; original retained",
        )
        return reference
    reference = _reference(
        attempt_id,
        task_key=task_key,
        closeout_route=closeout_route,
        status="conflict",
        path=str(path),
        reason="existing_ledger_identity_conflict",
        existing_identity={
            "task_id": existing.get("task_id"),
            "attempt_id": existing.get("attempt_id"),
            "attempt_number": existing.get("attempt_number"),
            "record_type": existing.get("record_type"),
            "schema_version": existing.get("schema_version"),
        },
        rejected_sha256=_digest(payload),
    )
    _record_event(
        db_path,
        task_key,
        reference,
        message="Outcome ledger conflict: existing artifact retained, new snapshot rejected",
    )
    return reference


def record_trigger_closeout_outcome(
    *,
    db_path: str | Path | None,
    task_key: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Post-commit writer for compatibility status-trigger Attempt closures.

    ``runtime_terminal_status_releases_lease`` can terminalize an active Attempt
    from a plain task-status write, which no Python admission callback observes.
    The caller records the Attempt that was active immediately before its own
    committed status write; this writer re-reads that exact Attempt and only
    publishes when it is genuinely terminal.
    """
    return record_terminal_attempt_outcome(
        db_path=db_path,
        attempt_id=attempt_id,
        closeout_route="task_status_trigger",
        task_key_hint=task_key,
        expected_task_key=task_key,
    )


def record_attempt_outcome_observation(
    *,
    db_path: str | Path | None,
    attempt_id: str,
    observation_type: str,
    observed_fields: dict[str, Any],
    source_reference: str,
    actor: str,
    expected_task_key: str | None = None,
) -> dict[str, Any]:
    """Append one later, actually observed outcome beside an existing ledger.

    The base ledger is never rewritten. The observation is a separate, uniquely
    identified artifact that names the base ledger and its digest. Only the
    fields that cannot exist at closeout may be enriched, and a base ledger must
    already exist: an observation never manufactures a ledger for an Attempt
    that was never terminalized.
    """
    unknown_fields = sorted(set(observed_fields) - set(ENRICHABLE_FIELDS))
    if unknown_fields:
        raise ValueError(
            f"Later observation may only enrich {ENRICHABLE_FIELDS}; got {unknown_fields}"
        )
    if not observed_fields:
        raise ValueError("Later observation must carry at least one observed field")
    resolved_db = Path(db_path) if db_path is not None else None
    try:
        return _record_attempt_outcome_observation(
            db_path=resolved_db,
            attempt_id=attempt_id,
            observation_type=observation_type,
            observed_fields=observed_fields,
            source_reference=source_reference,
            actor=actor,
            expected_task_key=expected_task_key,
        )
    except Exception as exc:  # pragma: no cover - observations never escalate
        logger.warning("Outcome ledger observation failed for %s: %s", attempt_id, exc)
        return _reference(
            attempt_id,
            task_key=expected_task_key,
            closeout_route="later_observation",
            status="incomplete",
            reason="outcome_observation_writer_error",
            error={"type": type(exc).__name__, "message": str(exc)},
        )


def _record_attempt_outcome_observation(
    *,
    db_path: Path | None,
    attempt_id: str,
    observation_type: str,
    observed_fields: dict[str, Any],
    source_reference: str,
    actor: str,
    expected_task_key: str | None,
) -> dict[str, Any]:
    binding: dict[str, str | None] = {"task_key": expected_task_key}

    def incomplete(reason: str, **extra: Any) -> dict[str, Any]:
        reference = _reference(
            attempt_id,
            task_key=binding["task_key"],
            closeout_route="later_observation",
            status="incomplete",
            reason=reason,
            **extra,
        )
        _record_event(
            db_path,
            binding["task_key"],
            reference,
            message=f"Outcome ledger observation incomplete: {reason}",
        )
        return reference

    snapshot = _read_snapshot(db_path, attempt_id)
    attempt = snapshot["attempt"]
    if attempt is None:
        return incomplete("attempt_row_not_found")
    task = snapshot["task"]
    task_key = _row_value(task, "task_key") if task is not None else expected_task_key
    binding["task_key"] = task_key
    if expected_task_key is not None and task_key != expected_task_key:
        return incomplete(
            "task_identity_mismatch",
            observed_task_key=task_key,
            expected_task_key=expected_task_key,
        )
    raw_root = attempt["artifact_root"]
    if not raw_root:
        return incomplete("attempt_artifact_root_missing")
    artifact_root = Path(raw_root)
    if not artifact_root.is_absolute() or ".." in artifact_root.parts:
        return incomplete("attempt_artifact_root_unsafe")

    base_name = ledger_filename(attempt_id)
    base, problem = _read_json_under_root(artifact_root, base_name)
    if problem is not None:
        return incomplete(f"base_ledger_{problem}")
    if not isinstance(base, dict) or base.get("attempt_id") != attempt_id:
        return incomplete("base_ledger_identity_mismatch")

    observation_id = uuid4().hex
    name = _observation_filename(attempt_id, observation_id)
    payload = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "kind": "attempt_outcome_observation",
        "record_type": OBSERVATION_RECORD_TYPE,
        "observation_id": observation_id,
        "observation_type": observation_type,
        "append_only": True,
        "overwrites_base_ledger": False,
        "lifecycle_authority": False,
        "merge_authority": False,
        "task_id": attempt["task_id"],
        "task_key": task_key,
        "attempt_id": attempt_id,
        "attempt_number": attempt["attempt_number"],
        "base_ledger": {
            "artifact_name": base_name,
            "path": str(artifact_root / base_name),
            "sha256": _digest(base),
            "schema_version": base.get("schema_version"),
        },
        "actor": actor,
        "source_reference": source_reference,
        "recorded_at": utc_now_iso(),
        "fields": {
            name: _field(
                value,
                "observed",
                source=source_reference,
                observed_by=actor,
                observation_id=observation_id,
            )
            for name, value in sorted(observed_fields.items())
        },
    }
    path = artifact_root / name
    try:
        publish_once(artifact_root, name, payload)
    except Exception as exc:
        return incomplete(
            "observation_publication_failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            intended_path=str(path),
        )
    index_error = _record_artifact(db_path, task_key, path)
    reference = _reference(
        attempt_id,
        task_key=task_key,
        closeout_route="later_observation",
        status="published" if index_error is None else "incomplete",
        path=str(path),
        reason=index_error,
        sha256=_digest(payload),
        observation_id=observation_id,
        observation_type=observation_type,
        observed_fields=sorted(observed_fields),
        base_ledger_path=str(artifact_root / base_name),
        artifact_indexed=index_error is None,
    )
    _record_event(
        db_path,
        task_key,
        reference,
        message=(
            f"Attempt outcome observation appended: {observation_type}"
            if index_error is None
            else f"Outcome observation published but not indexed: {index_error}"
        ),
    )
    return reference
