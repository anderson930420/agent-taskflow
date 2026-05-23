"""Intake-to-runner handoff contract.

This module produces a deterministic handoff artifact AFTER a scheduler
confirmation verifier report is valid, but BEFORE any runtime,
executor, validator, PR operation, merge, approval, cleanup, or
background worker is started. The handoff is the structural bridge
between the read-only scheduler confirmation surface and any future
runtime gate; it is itself NOT a runtime gate.

The handoff artifact is intentionally NOT action evidence and NOT
execution permission. Its existence does not authorize any executor,
validator, push, PR creation, merge, approval, rejection, or cleanup,
and it never starts a background worker. Any future runtime consumption
must still revalidate the bound proposal and the bound scheduler
confirmation, and must still require a separate command-specific
``--confirm-*`` operator gate.

The artifact and event types it may record
(``intake_runner_handoff`` / ``intake_runner_handoff_created``) are
intentionally disjoint from the workflow's action evidence types and
from scheduler_confirmation_consumption types.

In confirmed mode, this module ALSO persists the verifier report it
relied on as a sibling on-disk artifact at
``artifact_root/scheduler_confirmation_verifier_reports/<verifier_run_id>/verifier_report.json``
and stamps the handoff artifact + event payload with the
``verifier_run_id`` and ``verifier_report_path``. This binding lets a
future runtime preflight stage re-open the exact verifier report rather
than trusting the handoff artifact's own claim that the verifier
passed. The verifier report artifact is itself NOT action evidence and
carries safety flags that explicitly disclaim execution permission.

The scheduler confirmation verifier itself remains dry-run-only and
read-only; this module is what performs the optional on-disk
persistence in confirmed mode. In dry-run mode no verifier report
artifact, no handoff artifact, and no DB event is ever written.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_taskflow.models import utc_now_iso
from agent_taskflow.scheduler_confirmation_verifier import (
    STATUS_VALID,
    SchedulerConfirmationVerificationRequest,
    SchedulerConfirmationVerifierError,
    verify_scheduler_confirmation_item,
)
from agent_taskflow.store import TaskMirrorStore


SCHEMA_VERSION = "intake_runner_handoff.v1"
HANDOFF_SOURCE = "intake_runner_handoff"
HANDOFF_ARTIFACT_TYPE = "intake_runner_handoff"
HANDOFF_EVENT_TYPE = "intake_runner_handoff_created"

# Persisted verifier report artifact. The on-disk JSON wraps the
# verifier's in-memory report with binding metadata so a future runtime
# preflight stage can resolve verifier_report_path / verifier_run_id and
# verify the exact report still exists and is still valid. The verifier
# report artifact is NOT action evidence; its safety block explicitly
# denies execution permission.
VERIFIER_REPORT_ARTIFACT_TYPE = "scheduler_confirmation_verifier_report"
VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION = (
    "scheduler_confirmation_verifier_report_artifact.v1"
)

# Verifier report artifact safety block. Every flag below is emitted as
# the exact constant below so a downstream reader cannot misread the
# persisted verifier report as execution permission.
VERIFIER_REPORT_ARTIFACT_SAFETY_FLAGS: dict[str, bool] = {
    "dry_run_report_only": True,
    "execution_allowed": False,
    "execution_performed": False,
    "action_evidence_created": False,
    "executor_started": False,
    "validators_started": False,
}

STATUS_PREVIEW = "preview"
STATUS_CREATED = "created"
STATUS_BLOCKED = "blocked"

# The handoff artifact never authorizes any action. Every flag below
# describes the absence of mutation/action so a downstream reader cannot
# misinterpret the handoff payload as execution permission.
HANDOFF_SAFETY_FLAGS: dict[str, bool] = {
    "handoff_only": True,
    "will_execute": False,
    "will_push": False,
    "will_create_pr": False,
    "will_merge": False,
    "will_approve": False,
    "will_reject": False,
    "will_cleanup": False,
    "will_delete_branch": False,
    "will_delete_worktree": False,
    "will_mutate_github": False,
    "will_mutate_db_as_action": False,
    "will_start_background_worker": False,
}

# The runner contract block records that the runner MUST NOT start on
# the strength of this artifact alone. Every flag is always false; a
# future runtime gate must revalidate the bound confirmation before it
# is allowed to act.
RUNNER_CONTRACT_FLAGS: dict[str, bool] = {
    "runner_may_start": False,
    "execution_allowed": False,
    "execution_performed": False,
    "executor_started": False,
    "validators_started": False,
    "action_evidence_created": False,
    "requires_future_runtime_gate": True,
}

_HANDOFF_ID_TIMESTAMP = re.compile(r"[:\-Z]")


class IntakeRunnerHandoffError(RuntimeError):
    """Raised when an intake-to-runner handoff cannot be safely produced."""


@dataclass(frozen=True)
class IntakeRunnerHandoffRequest:
    """Inputs to one intake-to-runner handoff.

    Exactly one of ``confirmation_id``, ``confirmation_artifact_path``,
    or ``latest=True`` must be supplied. ``proposal_item_id`` selects the
    single confirmed item that the handoff describes.

    Dry-run by default. Persistence requires both ``dry_run=False`` and
    ``confirm_create_handoff=True``; either alone is rejected.
    """

    db_path: Path
    artifact_root: Path
    proposal_item_id: str
    confirmation_id: str | None = None
    confirmation_artifact_path: Path | None = None
    latest: bool = False
    expected_command_kind: str | None = None
    task_key: str | None = None
    max_age_minutes: int | None = None
    dry_run: bool = True
    confirm_create_handoff: bool = False
    now: datetime | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        artifact_root = Path(self.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        object.__setattr__(self, "artifact_root", artifact_root)

        if self.confirmation_artifact_path is not None:
            cap = Path(self.confirmation_artifact_path).expanduser()
            if not cap.is_absolute():
                raise ValueError(
                    "confirmation_artifact_path must be an absolute path"
                )
            object.__setattr__(self, "confirmation_artifact_path", cap)

        if self.confirmation_id is not None:
            cid = self.confirmation_id.strip()
            if not cid:
                raise ValueError("confirmation_id must not be empty")
            object.__setattr__(self, "confirmation_id", cid)

        item_id = self.proposal_item_id.strip() if self.proposal_item_id else ""
        if not item_id:
            raise ValueError("proposal_item_id must be a non-empty string")
        object.__setattr__(self, "proposal_item_id", item_id)

        if self.expected_command_kind is not None:
            kind = self.expected_command_kind.strip()
            object.__setattr__(
                self, "expected_command_kind", kind if kind else None
            )

        if self.task_key is not None:
            tk = self.task_key.strip()
            object.__setattr__(self, "task_key", tk if tk else None)

        if self.max_age_minutes is not None and self.max_age_minutes < 0:
            raise ValueError("max_age_minutes must be zero or positive")


def create_intake_runner_handoff(
    request: IntakeRunnerHandoffRequest,
) -> dict[str, Any]:
    """Produce an intake-to-runner handoff payload.

    Behavior:

    * Verify the scheduler confirmation via
      :func:`verify_scheduler_confirmation_item`. The verifier is
      read-only and never executes, mutates DB rows, or contacts
      GitHub.
    * If the verifier did not return a fully valid binding, refuse
      persistence and either:
        - in ``dry_run`` mode: return a structured ``blocked`` payload,
        - in confirmed mode: raise :class:`IntakeRunnerHandoffError`.
    * In ``dry_run`` mode with a valid verifier: return the handoff
      payload only; never write an artifact and never record any DB
      event.
    * In confirmed mode with a valid verifier: write the handoff JSON
      under ``artifact_root/intake_runner_handoffs/<handoff_id>/`` and
      record only ``intake_runner_handoff`` artifact / event evidence.
      No executor is started, no validators run, no PR/branch/merge/
      cleanup operation is performed.

    The returned payload always carries ``execution_allowed=false``,
    ``execution_performed=false``, ``executor_started=false``, and
    ``action_evidence_created=false``.
    """

    if not request.dry_run and not request.confirm_create_handoff:
        raise IntakeRunnerHandoffError(
            "non-dry-run intake-to-runner handoff requires "
            "confirm_create_handoff=True"
        )

    selectors = sum(
        1
        for selector in (
            request.confirmation_id is not None,
            request.confirmation_artifact_path is not None,
            request.latest,
        )
        if selector
    )
    if selectors == 0:
        raise IntakeRunnerHandoffError(
            "intake-to-runner handoff requires one of confirmation_id, "
            "confirmation_artifact_path, or latest=True"
        )
    if selectors > 1:
        raise IntakeRunnerHandoffError(
            "intake-to-runner handoff accepts only one of confirmation_id, "
            "confirmation_artifact_path, latest"
        )

    verifier_request = SchedulerConfirmationVerificationRequest(
        db_path=request.db_path,
        proposal_item_id=request.proposal_item_id,
        artifact_root=request.artifact_root,
        confirmation_id=request.confirmation_id,
        confirmation_artifact_path=request.confirmation_artifact_path,
        latest=request.latest,
        expected_command_kind=request.expected_command_kind,
        task_key=request.task_key,
        max_age_minutes=request.max_age_minutes,
        now=request.now,
    )

    try:
        verifier_report = verify_scheduler_confirmation_item(verifier_request)
    except SchedulerConfirmationVerifierError as exc:
        raise IntakeRunnerHandoffError(
            f"could not verify scheduler confirmation: {exc}"
        ) from exc

    verifier_valid = _verifier_is_valid(verifier_report)

    if not verifier_valid:
        if not request.dry_run:
            raise IntakeRunnerHandoffError(
                "intake-to-runner handoff refused: scheduler confirmation "
                f"verifier did not pass "
                f"(status={verifier_report.get('status')!r}, "
                f"verification_passed={verifier_report.get('verification_passed')!r})"
            )
        return _blocked_payload(request=request, verifier_report=verifier_report)

    handoff_id = _make_handoff_id()
    created_at = utc_now_iso()
    mode = "dry_run" if request.dry_run else "confirmed"

    if request.dry_run:
        # Dry-run preview MUST NOT write either the verifier report
        # artifact or the handoff artifact and MUST NOT touch the DB.
        # verifier_run_id and verifier_report_path are surfaced as
        # ``None`` so the preview payload cannot be mistaken for a
        # persisted handoff.
        payload = _build_payload(
            request=request,
            verifier_report=verifier_report,
            handoff_id=handoff_id,
            created_at=created_at,
            mode=mode,
            status=STATUS_PREVIEW,
            artifact_path=None,
            verifier_run_id=None,
            verifier_report_path=None,
        )
        return payload

    verifier_run_id = _make_verifier_run_id()
    verifier_report_path = (
        request.artifact_root
        / "scheduler_confirmation_verifier_reports"
        / verifier_run_id
        / "verifier_report.json"
    )
    artifact_path = (
        request.artifact_root
        / "intake_runner_handoffs"
        / handoff_id
        / "intake_runner_handoff.json"
    )

    verifier_report_artifact = _build_verifier_report_artifact(
        verifier_report=verifier_report,
        verifier_run_id=verifier_run_id,
        created_at=created_at,
    )
    verifier_report_path.parent.mkdir(parents=True, exist_ok=True)
    verifier_report_path.write_text(
        json.dumps(verifier_report_artifact, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    payload = _build_payload(
        request=request,
        verifier_report=verifier_report,
        handoff_id=handoff_id,
        created_at=created_at,
        mode=mode,
        status=STATUS_CREATED,
        artifact_path=artifact_path,
        verifier_run_id=verifier_run_id,
        verifier_report_path=verifier_report_path,
    )

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    task_key = verifier_report.get("task_key")
    if isinstance(task_key, str) and task_key:
        store = TaskMirrorStore(request.db_path)
        store.record_task_artifact(
            task_key,
            HANDOFF_ARTIFACT_TYPE,
            artifact_path,
        )
        store.record_task_event(
            task_key,
            HANDOFF_EVENT_TYPE,
            HANDOFF_SOURCE,
            message=(
                f"Intake-to-runner handoff {handoff_id} prepared for "
                f"{verifier_report.get('recommended_command_kind')} "
                "(handoff only; no execution)"
            ),
            payload={
                "kind": HANDOFF_EVENT_TYPE,
                "schema_version": SCHEMA_VERSION,
                "handoff_id": handoff_id,
                "handoff_artifact_path": str(artifact_path),
                "confirmation_id": verifier_report.get("confirmation_id"),
                "confirmation_artifact_path": verifier_report.get(
                    "confirmation_artifact_path"
                ),
                "proposal_id": verifier_report.get("proposal_id"),
                "proposal_hash": verifier_report.get("proposal_hash"),
                "proposal_item_id": verifier_report.get("proposal_item_id"),
                "item_hash": verifier_report.get("item_hash"),
                "task_key": task_key,
                "recommended_command_kind": verifier_report.get(
                    "recommended_command_kind"
                ),
                "verifier_run_id": verifier_run_id,
                "verifier_report_path": str(verifier_report_path),
                "verifier_report_artifact_type": (
                    VERIFIER_REPORT_ARTIFACT_TYPE
                ),
                "verifier_report_schema_version": (
                    VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION
                ),
                "handoff_only": True,
                "execution_allowed": False,
                "execution_performed": False,
                "executor_started": False,
                "validators_started": False,
                "action_evidence_created": False,
                "requires_future_runtime_gate": True,
            },
        )

    return payload


def _verifier_is_valid(verifier_report: dict[str, Any]) -> bool:
    return (
        verifier_report.get("status") == STATUS_VALID
        and bool(verifier_report.get("verification_passed"))
        and bool(verifier_report.get("eligible_for_command_specific_confirm"))
        and verifier_report.get("execution_allowed") is False
        and verifier_report.get("execution_performed") is False
        and verifier_report.get("action_evidence_created") is False
    )


def _build_payload(
    *,
    request: IntakeRunnerHandoffRequest,
    verifier_report: dict[str, Any],
    handoff_id: str,
    created_at: str,
    mode: str,
    status: str,
    artifact_path: Path | None,
    verifier_run_id: str | None,
    verifier_report_path: Path | None,
) -> dict[str, Any]:
    return {
        "ok": status != STATUS_BLOCKED,
        "status": status,
        "schema_version": SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "created_at": created_at,
        "source": HANDOFF_SOURCE,
        "mode": mode,
        "db_path": str(request.db_path),
        "artifact_root": str(request.artifact_root),
        "artifact_path": str(artifact_path) if artifact_path else None,
        "task_key": verifier_report.get("task_key"),
        "recommended_command_kind": verifier_report.get(
            "recommended_command_kind"
        ),
        "proposal": _proposal_block(verifier_report),
        "confirmation": _confirmation_block(verifier_report),
        "runner_contract": dict(RUNNER_CONTRACT_FLAGS),
        "safety": dict(HANDOFF_SAFETY_FLAGS),
        "verifier_report": _verifier_report_block(
            verifier_report=verifier_report,
            verifier_run_id=verifier_run_id,
            verifier_report_path=verifier_report_path,
            persisted=status == STATUS_CREATED,
        ),
        "verifier_report_summary": _verifier_report_summary(verifier_report),
    }


def _blocked_payload(
    *,
    request: IntakeRunnerHandoffRequest,
    verifier_report: dict[str, Any],
) -> dict[str, Any]:
    payload = _build_payload(
        request=request,
        verifier_report=verifier_report,
        handoff_id=_make_handoff_id(),
        created_at=utc_now_iso(),
        mode="dry_run",
        status=STATUS_BLOCKED,
        artifact_path=None,
        verifier_run_id=None,
        verifier_report_path=None,
    )
    payload["error"] = _verifier_error_description(verifier_report)
    return payload


def _build_verifier_report_artifact(
    *,
    verifier_report: dict[str, Any],
    verifier_run_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Wrap the verifier's in-memory report with binding metadata.

    The wrapper preserves the entire verifier_report payload under
    ``report`` so a future runtime preflight stage can re-validate every
    check the verifier ran. The wrapper itself adds the verifier_run_id,
    creation timestamp, source, and a safety block that explicitly
    denies execution permission so the persisted artifact cannot be
    misread as action evidence.
    """

    return {
        "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
        "verifier_run_id": verifier_run_id,
        "created_at": created_at,
        "source": HANDOFF_SOURCE,
        "report": dict(verifier_report),
        "safety": dict(VERIFIER_REPORT_ARTIFACT_SAFETY_FLAGS),
    }


def _verifier_report_block(
    *,
    verifier_report: dict[str, Any],
    verifier_run_id: str | None,
    verifier_report_path: Path | None,
    persisted: bool,
) -> dict[str, Any]:
    """Construct the handoff artifact's verifier binding block.

    When ``persisted`` is True, ``verifier_run_id`` and
    ``verifier_report_path`` describe the on-disk verifier report
    artifact that future runtime preflight must reopen. When False (dry
    run preview or blocked dry run), both fields are ``None`` because
    no verifier report artifact was written.
    """

    return {
        "verifier_run_id": verifier_run_id,
        "verifier_report_path": (
            str(verifier_report_path) if verifier_report_path else None
        ),
        "artifact_type": VERIFIER_REPORT_ARTIFACT_TYPE,
        "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
        "persisted": persisted,
        "status": verifier_report.get("status"),
        "verification_passed": bool(
            verifier_report.get("verification_passed")
        ),
        "eligible_for_command_specific_confirm": bool(
            verifier_report.get("eligible_for_command_specific_confirm")
        ),
        "execution_allowed": False,
        "execution_performed": False,
        "action_evidence_created": False,
        "expiration": verifier_report.get("expiration"),
    }


def _proposal_block(verifier_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": verifier_report.get("proposal_id"),
        "proposal_hash": verifier_report.get("proposal_hash"),
        "proposal_artifact_path": verifier_report.get("proposal_artifact_path"),
        "proposal_item_id": verifier_report.get("proposal_item_id"),
        "item_hash": verifier_report.get("item_hash"),
    }


def _confirmation_block(verifier_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "confirmation_id": verifier_report.get("confirmation_id"),
        "confirmation_artifact_path": verifier_report.get(
            "confirmation_artifact_path"
        ),
        "verification_status": verifier_report.get("status"),
        "verification_passed": bool(verifier_report.get("verification_passed")),
        "eligible_for_command_specific_confirm": bool(
            verifier_report.get("eligible_for_command_specific_confirm")
        ),
    }


def _verifier_report_summary(
    verifier_report: dict[str, Any],
) -> dict[str, Any]:
    checks = verifier_report.get("checks") or []
    failed_checks: list[str] = []
    for check in checks:
        if isinstance(check, dict) and not check.get("passed"):
            name = check.get("name")
            if isinstance(name, str):
                failed_checks.append(name)
    return {
        "schema_version": verifier_report.get("schema_version"),
        "status": verifier_report.get("status"),
        "verification_passed": bool(verifier_report.get("verification_passed")),
        "eligible_for_command_specific_confirm": bool(
            verifier_report.get("eligible_for_command_specific_confirm")
        ),
        "execution_allowed": False,
        "execution_performed": False,
        "action_evidence_created": False,
        "failed_check_count": len(failed_checks),
        "failed_check_names": failed_checks,
        "expiration": verifier_report.get("expiration"),
    }


def _verifier_error_description(verifier_report: dict[str, Any]) -> str:
    failed_names: list[str] = []
    for check in verifier_report.get("checks") or []:
        if isinstance(check, dict) and not check.get("passed"):
            name = check.get("name")
            if isinstance(name, str):
                failed_names.append(name)
    status = verifier_report.get("status")
    if failed_names:
        return (
            f"scheduler confirmation verifier did not pass (status={status!r}); "
            f"failed checks: {', '.join(failed_names)}"
        )
    return f"scheduler confirmation verifier did not pass (status={status!r})"


def _make_handoff_id() -> str:
    timestamp = _HANDOFF_ID_TIMESTAMP.sub("", utc_now_iso())
    return f"handoff-{timestamp}-{secrets.token_hex(6)}"


def _make_verifier_run_id() -> str:
    timestamp = _HANDOFF_ID_TIMESTAMP.sub("", utc_now_iso())
    return f"verifier-run-{timestamp}-{secrets.token_hex(6)}"


__all__ = [
    "HANDOFF_ARTIFACT_TYPE",
    "HANDOFF_EVENT_TYPE",
    "HANDOFF_SAFETY_FLAGS",
    "HANDOFF_SOURCE",
    "RUNNER_CONTRACT_FLAGS",
    "SCHEMA_VERSION",
    "STATUS_BLOCKED",
    "STATUS_CREATED",
    "STATUS_PREVIEW",
    "VERIFIER_REPORT_ARTIFACT_SAFETY_FLAGS",
    "VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION",
    "VERIFIER_REPORT_ARTIFACT_TYPE",
    "IntakeRunnerHandoffError",
    "IntakeRunnerHandoffRequest",
    "create_intake_runner_handoff",
]
