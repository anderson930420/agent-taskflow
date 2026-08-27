"""Read-only Attempt binding for Level 2 PR preparation.

Normal execution reaches PR preparation with a bound canonical Attempt. An
audited advisory-evidence recovery reaches the same task status with the exact
Attempt identified but deliberately unbound. This module keeps those two
limited paths explicit without allowing a downstream publisher to select a
different Attempt from storage.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from agent_taskflow.advisory_evidence_retry import (
    RETRY_AUDIT_KIND,
    verify_advisory_evidence_retry_audit,
)
from agent_taskflow.level2_execution_authority import (
    Level2ExecutionAuthorityError,
    is_level2_task,
    verify_canonical_attempt,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


NORMAL_PATH = "bound_attempt"
RECOVERY_PATH = "audited_recovery"
LEGACY_PATH = "not_level2"


@dataclass(frozen=True)
class PRPreparationAttemptBinding:
    """Verified runtime and Attempt evidence used by publication entrypoints."""

    level2_task: bool
    attempt_id: str | None
    path: str | None
    canonical_attempt_verified: bool
    recovery_operator: str | None
    artifact_count: int
    finished_event_count: int
    runner_ok: bool | None
    runtime_evidence_found: bool
    reasons: tuple[str, ...]

    def to_summary(self) -> dict[str, Any]:
        return {
            "runtime_evidence_found": self.runtime_evidence_found,
            "artifact_count": self.artifact_count,
            "finished_event_count": self.finished_event_count,
            "runner_ok": self.runner_ok,
            "canonical_attempt_id": self.attempt_id,
            "canonical_attempt_verified": self.canonical_attempt_verified,
            "canonical_attempt_path": self.path,
            "recovery_operator": self.recovery_operator,
        }


def resolve_pr_preparation_attempt_binding(
    *,
    db_path: Path,
    task_key: str,
    requested_attempt_id: str | None = None,
    store: TaskMirrorStore | None = None,
) -> PRPreparationAttemptBinding:
    """Resolve one exact Level 2 Attempt from persisted runtime evidence.

    A caller may repeat the id reported by the runtime path, but cannot use it
    to override that evidence. The recovery exception is intentionally narrow:
    it requires every runtime payload to identify the same unbound Attempt,
    that Attempt to pass canonical store verification, and exactly one matching
    #184 recovery audit event for the Attempt artifact directory.
    """

    normalized_task_key = normalize_task_key(task_key)
    current_store = store or TaskMirrorStore(db_path)
    reasons: list[str] = []
    try:
        level2_task = is_level2_task(db_path, normalized_task_key)
    except Level2ExecutionAuthorityError as exc:
        return _binding_failure(
            level2_task=True,
            reasons=[f"canonical_attempt_store_verification_failed: {exc}"],
        )

    artifacts = [
        artifact
        for artifact in current_store.list_task_artifacts(normalized_task_key)
        if artifact.artifact_type == RUNTIME_EXECUTION_ARTIFACT_TYPE
    ]
    events = [
        event
        for event in current_store.list_task_events(normalized_task_key)
        if event.event_type == RUNTIME_FINISHED_EVENT_TYPE
    ]
    if not artifacts:
        reasons.append("runtime_handoff_execution_artifact_missing")
    if not events:
        reasons.append("runtime_execution_finished_event_missing")

    payloads: list[dict[str, Any]] = []
    for artifact in artifacts:
        payload, payload_reasons = _read_json_artifact(artifact.path)
        reasons.extend(payload_reasons)
        if payload is not None:
            payloads.append(payload)
    for event in events:
        payload = _event_payload(event.payload_json)
        if payload is not None:
            payloads.append(payload)

    runner_values = [
        payload["runner_ok"]
        for payload in payloads
        if isinstance(payload.get("runner_ok"), bool)
    ]
    runner_ok = runner_values[-1] if runner_values else None

    if not level2_task:
        if any(value is False for value in runner_values):
            reasons.append("runtime_runner_not_ok")
        return _binding_result(
            level2_task=False,
            attempt_id=None,
            path=LEGACY_PATH,
            artifact_count=len(artifacts),
            finished_event_count=len(events),
            runner_ok=runner_ok,
            reasons=reasons,
        )

    evidence_attempt_ids = {
        payload["canonical_attempt_id"].strip()
        for payload in payloads
        if isinstance(payload.get("canonical_attempt_id"), str)
        and payload["canonical_attempt_id"].strip()
    }
    if len(evidence_attempt_ids) != 1:
        reasons.append(
            "runtime_canonical_attempt_id_missing"
            if not evidence_attempt_ids
            else "runtime_canonical_attempt_evidence_ambiguous"
        )
        return _binding_result(
            level2_task=True,
            attempt_id=None,
            path=None,
            artifact_count=len(artifacts),
            finished_event_count=len(events),
            runner_ok=runner_ok,
            reasons=reasons,
        )

    attempt_id = next(iter(evidence_attempt_ids))
    requested = str(requested_attempt_id or "").strip()
    if requested and requested != attempt_id:
        reasons.append("runtime_canonical_attempt_id_mismatch")

    for payload in payloads:
        if payload.get("execution_authority") != "execution_engine":
            reasons.append("runtime_execution_authority_invalid")
        if payload.get("canonical_attempt_id") != attempt_id:
            reasons.append("runtime_canonical_attempt_id_mismatch")

    verification = None
    try:
        verification = verify_canonical_attempt(
            db_path=db_path,
            task_key=normalized_task_key,
            attempt_id=attempt_id,
        )
    except Level2ExecutionAuthorityError as exc:
        reasons.append(f"canonical_attempt_invalid: {exc}")

    if all(value is True for value in runner_values) and runner_values:
        for payload in payloads:
            if payload.get("canonical_attempt_bound") is not True:
                reasons.append("runtime_canonical_attempt_not_bound")
            if payload.get("canonical_attempt_store_verified") is not True:
                reasons.append("runtime_canonical_attempt_not_store_verified")
        return _binding_result(
            level2_task=True,
            attempt_id=attempt_id,
            path=NORMAL_PATH,
            canonical_attempt_verified=verification is not None,
            artifact_count=len(artifacts),
            finished_event_count=len(events),
            runner_ok=runner_ok,
            reasons=reasons,
        )

    if not runner_values:
        reasons.append("runtime_runner_ok_missing")
    elif not all(value is False for value in runner_values):
        reasons.append("runtime_runner_evidence_conflict")
    else:
        for payload in payloads:
            if payload.get("canonical_attempt_bound") is not False:
                reasons.append("runtime_canonical_attempt_not_identified_recovery")
        recovery_operator, recovery_reasons = _recovery_audit(
            current_store,
            task_key=normalized_task_key,
            attempt_id=attempt_id,
            artifact_dir=(verification.attempt.artifact_root if verification else None),
        )
        reasons.extend(recovery_reasons)
        if recovery_reasons:
            reasons.append("runtime_runner_not_ok")
        return _binding_result(
            level2_task=True,
            attempt_id=attempt_id,
            path=RECOVERY_PATH,
            canonical_attempt_verified=verification is not None,
            recovery_operator=recovery_operator,
            artifact_count=len(artifacts),
            finished_event_count=len(events),
            runner_ok=runner_ok,
            reasons=reasons,
        )

    return _binding_result(
        level2_task=True,
        attempt_id=attempt_id,
        path=None,
        canonical_attempt_verified=verification is not None,
        artifact_count=len(artifacts),
        finished_event_count=len(events),
        runner_ok=runner_ok,
        reasons=reasons,
    )


def _recovery_audit(
    store: TaskMirrorStore,
    *,
    task_key: str,
    attempt_id: str,
    artifact_dir: Path | None,
) -> tuple[str | None, list[str]]:
    if artifact_dir is None:
        return None, ["canonical_attempt_artifact_dir_missing"]

    valid_operators: list[str] = []
    invalid_events: list[str] = []
    for event in store.list_task_events(task_key):
        payload = _event_payload(event.payload_json)
        if payload is None or payload.get("kind") != RETRY_AUDIT_KIND:
            continue
        verification = verify_advisory_evidence_retry_audit(
            payload,
            task_key=task_key,
            attempt_id=attempt_id,
            artifact_dir=artifact_dir,
        )
        if verification.valid:
            assert verification.operator is not None
            valid_operators.append(verification.operator)
        else:
            invalid_events.extend(verification.reasons)

    if len(valid_operators) == 1:
        return valid_operators[0], []
    if len(valid_operators) > 1:
        return None, ["recovery_audit_event_ambiguous"]
    if invalid_events:
        return None, ["recovery_audit_event_invalid"]
    return None, ["recovery_audit_event_missing"]


def _read_json_artifact(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"runtime_handoff_execution_artifact_file_missing: {path}"]
    except OSError as exc:
        return None, [f"runtime_handoff_execution_artifact_read_error: {exc}"]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, [f"runtime_handoff_execution_artifact_json_malformed: {path}"]
    if not isinstance(payload, dict):
        return None, [f"runtime_handoff_execution_artifact_json_not_object: {path}"]
    return payload, []


def _event_payload(payload_json: str | None) -> dict[str, Any] | None:
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _binding_failure(
    *,
    level2_task: bool,
    reasons: list[str],
) -> PRPreparationAttemptBinding:
    return _binding_result(
        level2_task=level2_task,
        attempt_id=None,
        path=None,
        artifact_count=0,
        finished_event_count=0,
        runner_ok=None,
        reasons=reasons,
    )


def _binding_result(
    *,
    level2_task: bool,
    attempt_id: str | None,
    path: str | None,
    artifact_count: int,
    finished_event_count: int,
    runner_ok: bool | None,
    reasons: list[str],
    canonical_attempt_verified: bool = False,
    recovery_operator: str | None = None,
) -> PRPreparationAttemptBinding:
    unique_reasons = tuple(dict.fromkeys(reasons))
    return PRPreparationAttemptBinding(
        level2_task=level2_task,
        attempt_id=attempt_id,
        path=path,
        canonical_attempt_verified=canonical_attempt_verified,
        recovery_operator=recovery_operator,
        artifact_count=artifact_count,
        finished_event_count=finished_event_count,
        runner_ok=runner_ok,
        runtime_evidence_found=bool(
            artifact_count and finished_event_count and not unique_reasons
        ),
        reasons=unique_reasons,
    )


__all__ = [
    "LEGACY_PATH",
    "NORMAL_PATH",
    "RECOVERY_PATH",
    "PRPreparationAttemptBinding",
    "resolve_pr_preparation_attempt_binding",
]
