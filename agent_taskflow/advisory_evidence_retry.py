"""Operator recovery for ``codex_advisory_evidence`` blocking.

The ``v0.2.5`` required Codex advisory evidence gate blocks
``run_approved_task`` at the ``codex_advisory_evidence`` phase when
``codex-advisory-review.json`` is missing from the Attempt artifact directory.
The advisory artifact can only be generated *after* the runner has produced
attempt evidence, so a first run always blocks, and ``reset_task_status.py``
only supports ``blocked -> queued`` while reserving a *new* Attempt with a
*new* artifact directory. Advisory evidence generated for the previous Attempt
is therefore never observed by the gate, which leaves an otherwise complete
task stuck in ``blocked``.

This module is the explicit, audited operator recovery entry point for exactly
that situation. It performs the deterministic ``blocked -> waiting_approval``
transition the runner would have performed, but only when every precondition is
verified against the *existing* Attempt artifact directory:

    a. the task exists and its current status is ``blocked``;
    b. the artifact directory carries executor evidence and passing
       deterministic pytest validator evidence;
    c. the existing ``v0.2.5`` gate helper passes against that artifact
       directory.

The core semantic is::

    Re-check the same evidence, do not weaken the gate.

Contract validation is never reimplemented here: precondition (c) delegates to
``check_required_codex_advisory_evidence``, so the advisory artifact must
satisfy exactly the same contract the runner requires. This module only reads
files and the local SQLite mirror. It never invokes Codex, runs a subprocess,
approves, merges, pushes, creates PRs, cleans up, deletes branches or
worktrees, mutates approval records, or reserves a new Attempt. Reaching
``waiting_approval`` is not approval; human final approval is always required.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_taskflow.codex_advisory_evidence_gate import (
    REQUIREMENT_NAME,
    RequiredCodexAdvisoryEvidenceRequest,
    RequiredCodexAdvisoryEvidenceResult,
    check_required_codex_advisory_evidence,
)
from agent_taskflow.codex_advisory_review import detect_evidence
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


RETRY_FROM_STATUS = "blocked"
RETRY_TO_STATUS = "waiting_approval"
RETRY_REASON = "advisory_evidence_retry"
RETRY_SOURCE = "advisory_evidence_retry_cli"
RETRY_EVENT_TYPE = "note"
RETRY_AUDIT_KIND = "advisory_evidence_retry"

PYTEST_LOG_FILENAME = "pytest.log"
PYTEST_LAUNCH_SPEC_FILENAME = "validator-launch-spec-pytest.json"

CHECK_TASK_BLOCKED = "task_blocked"
CHECK_EXECUTOR_EVIDENCE = "executor_evidence"
CHECK_PYTEST_EVIDENCE = "pytest_validator_evidence"
CHECK_ADVISORY_EVIDENCE = REQUIREMENT_NAME

# Executor evidence is detected generically so the recovery path stays
# executor-neutral (manual, shell, pi, opencode, Claude Code). Detection is
# file presence only; file contents are never interpreted as executor results.
EXECUTOR_LAUNCH_SPEC_GLOB = "executor-launch-spec-*.json"
EXECUTOR_EVIDENCE_FILES = (
    "mission_contract.json",
    "task_execution_package.json",
)

# Terminal pytest summary counts, e.g.
# "=========== 4299 passed, 8 skipped in 373.48s (0:06:13) ============".
_PYTEST_COUNT_PATTERN = re.compile(r"(?<!\w)(\d+)\s+([a-z]+)")
_PYTEST_FAILING_LABELS = ("failed", "error", "errors")
_PYTEST_SUMMARY_LABELS = (
    "passed",
    "failed",
    "error",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "deselected",
    "warning",
    "warnings",
)


class AdvisoryEvidenceRetryError(RuntimeError):
    """Raised when an audited advisory-evidence retry cannot proceed safely."""


@dataclass(frozen=True)
class AdvisoryEvidenceRetryRequest:
    """Validated request for one operator advisory-evidence retry transition."""

    task_key: str
    artifact_dir: Path
    operator: str
    db_path: Path | None = None
    confirm_transition: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        normalized_operator = self.operator.strip()
        if not normalized_operator:
            raise ValueError("operator must not be empty")
        object.__setattr__(self, "operator", normalized_operator)

        object.__setattr__(
            self,
            "artifact_dir",
            Path(self.artifact_dir).expanduser().resolve(),
        )

        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                Path(self.db_path).expanduser().resolve(),
            )


@dataclass(frozen=True)
class PreconditionCheck:
    """One deterministic precondition outcome for operator reporting."""

    name: str
    satisfied: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "satisfied": self.satisfied,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class AdvisoryEvidenceRetryResult:
    """Structured dry-run report or performed-transition result."""

    task_key: str
    artifact_dir: Path
    operator: str
    reason: str
    from_status: str
    to_status: str
    observed_status: str | None
    confirm_transition: bool
    preconditions_satisfied: bool
    checks: tuple[PreconditionCheck, ...]
    blocking_errors: tuple[str, ...]
    mutated: bool
    audit_event_recorded: bool
    codex_advisory_evidence: dict[str, Any]

    @property
    def ok(self) -> bool:
        """True when the requested operation completed as asked."""

        if self.confirm_transition:
            return self.mutated
        return self.preconditions_satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "artifact_dir": str(self.artifact_dir),
            "operator": self.operator,
            "reason": self.reason,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "observed_status": self.observed_status,
            "confirm_transition": self.confirm_transition,
            "dry_run": not self.confirm_transition,
            "preconditions_satisfied": self.preconditions_satisfied,
            "checks": [check.to_dict() for check in self.checks],
            "blocking_errors": list(self.blocking_errors),
            "mutated": self.mutated,
            "audit_event_recorded": self.audit_event_recorded,
            "codex_advisory_evidence": dict(self.codex_advisory_evidence),
            "ok": self.ok,
            "requires_human_review": True,
            "not_approval": True,
            "not_merge": True,
            "not_cleanup": True,
            "not_validation_authority": True,
        }


def summarize_pytest_log(log_path: Path) -> tuple[bool, str | None, str | None]:
    """Return ``(passing, summary_line, error)`` for a pytest validator log.

    Reads the log only. The log is treated as passing when its last terminal
    counts summary reports at least one ``passed`` and reports no ``failed`` or
    ``error`` counts. A log without a terminal counts summary (a truncated or
    still-running validator log) is never treated as passing.
    """

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, None, f"{exc.__class__.__name__}: {exc}"

    summary_line: str | None = None
    counts: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        body = line.strip("=").strip()
        if not body:
            continue
        line_counts = {
            label: int(count)
            for count, label in _PYTEST_COUNT_PATTERN.findall(body)
            if label in _PYTEST_SUMMARY_LABELS
        }
        if not line_counts:
            continue
        summary_line = line
        counts = line_counts

    if summary_line is None:
        return (
            False,
            None,
            f"{log_path.name} has no pytest terminal summary line",
        )

    failing = {
        label: counts[label] for label in _PYTEST_FAILING_LABELS if counts.get(label)
    }
    if failing:
        detail = ", ".join(f"{count} {label}" for label, count in failing.items())
        return False, summary_line, f"{log_path.name} reports {detail}"
    if not counts.get("passed"):
        return (
            False,
            summary_line,
            f"{log_path.name} summary reports no passed tests",
        )
    return True, summary_line, None


def _check_task_status(
    store: TaskMirrorStore, task_key: str
) -> tuple[PreconditionCheck, str | None]:
    task = store.get_task(task_key)
    if task is None:
        return (
            PreconditionCheck(
                name=CHECK_TASK_BLOCKED,
                satisfied=False,
                summary=f"Task {task_key} was not found in the state DB",
                details={"task_found": False, "observed_status": None},
            ),
            None,
        )
    if task.status != RETRY_FROM_STATUS:
        return (
            PreconditionCheck(
                name=CHECK_TASK_BLOCKED,
                satisfied=False,
                summary=(
                    f"Task {task_key} status is {task.status!r}; expected "
                    f"{RETRY_FROM_STATUS!r}"
                ),
                details={"task_found": True, "observed_status": task.status},
            ),
            task.status,
        )
    return (
        PreconditionCheck(
            name=CHECK_TASK_BLOCKED,
            satisfied=True,
            summary=f"Task {task_key} is {RETRY_FROM_STATUS}",
            details={"task_found": True, "observed_status": task.status},
        ),
        task.status,
    )


def _check_executor_evidence(artifact_dir: Path) -> PreconditionCheck:
    """Check that the Attempt artifact directory carries executor evidence."""

    if not artifact_dir.is_dir():
        return PreconditionCheck(
            name=CHECK_EXECUTOR_EVIDENCE,
            satisfied=False,
            summary=f"Attempt artifact dir does not exist: {artifact_dir}",
            details={"artifact_dir_exists": False, "signals": []},
        )

    manifest = detect_evidence(artifact_dir)
    executor_logs = [item["name"] for item in manifest["executor_logs"]]
    launch_specs = sorted(
        path.name
        for path in artifact_dir.glob(EXECUTOR_LAUNCH_SPEC_GLOB)
        if path.is_file()
    )
    package_files = [
        name
        for name in EXECUTOR_EVIDENCE_FILES
        if (artifact_dir / name).is_file()
    ]

    signals = [*launch_specs, *executor_logs, *package_files]
    details = {
        "artifact_dir_exists": True,
        "executor_launch_specs": launch_specs,
        "executor_logs": executor_logs,
        "execution_package_files": package_files,
        "signals": signals,
    }
    if not signals:
        return PreconditionCheck(
            name=CHECK_EXECUTOR_EVIDENCE,
            satisfied=False,
            summary=(
                "Attempt artifact dir carries no executor evidence (no executor "
                "launch spec, executor log, or execution package artifact)"
            ),
            details=details,
        )
    return PreconditionCheck(
        name=CHECK_EXECUTOR_EVIDENCE,
        satisfied=True,
        summary=f"Executor evidence present ({len(signals)} artifact(s))",
        details=details,
    )


def _check_pytest_evidence(artifact_dir: Path) -> PreconditionCheck:
    """Check for passing deterministic pytest validator evidence."""

    log_path = artifact_dir / PYTEST_LOG_FILENAME
    spec_path = artifact_dir / PYTEST_LAUNCH_SPEC_FILENAME
    details: dict[str, Any] = {
        "pytest_log_path": str(log_path) if log_path.is_file() else None,
        "validator_launch_spec_path": (
            str(spec_path) if spec_path.is_file() else None
        ),
        "pytest_summary_line": None,
    }

    errors: list[str] = []
    if not log_path.is_file():
        errors.append(f"{PYTEST_LOG_FILENAME} is missing in {artifact_dir}")
    else:
        passing, summary_line, error = summarize_pytest_log(log_path)
        details["pytest_summary_line"] = summary_line
        details["pytest_passed"] = passing
        if error is not None:
            errors.append(error)

    if not spec_path.is_file():
        errors.append(f"{PYTEST_LAUNCH_SPEC_FILENAME} is missing in {artifact_dir}")
    else:
        try:
            spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{PYTEST_LAUNCH_SPEC_FILENAME} could not be parsed: {exc}")
        else:
            if not isinstance(spec_data, dict):
                errors.append(
                    f"{PYTEST_LAUNCH_SPEC_FILENAME} is not a JSON object"
                )

    details["errors"] = errors
    if errors:
        return PreconditionCheck(
            name=CHECK_PYTEST_EVIDENCE,
            satisfied=False,
            summary="; ".join(errors),
            details=details,
        )
    return PreconditionCheck(
        name=CHECK_PYTEST_EVIDENCE,
        satisfied=True,
        summary=(
            "Deterministic pytest validator evidence is present and passing: "
            f"{details['pytest_summary_line']}"
        ),
        details=details,
    )


def _check_advisory_evidence(
    artifact_dir: Path, task_key: str
) -> tuple[PreconditionCheck, RequiredCodexAdvisoryEvidenceResult]:
    """Delegate to the existing v0.2.5 gate helper; never reimplement it."""

    evidence = check_required_codex_advisory_evidence(
        RequiredCodexAdvisoryEvidenceRequest(
            artifact_dir=artifact_dir,
            task_key=task_key,
        )
    )
    if not evidence.satisfied:
        return (
            PreconditionCheck(
                name=CHECK_ADVISORY_EVIDENCE,
                satisfied=False,
                summary=evidence.blocking_summary(),
                details=evidence.to_dict(),
            ),
            evidence,
        )
    return (
        PreconditionCheck(
            name=CHECK_ADVISORY_EVIDENCE,
            satisfied=True,
            summary=(
                "Codex advisory artifact contract evidence is present "
                f"(review_status={evidence.review_status!r}, "
                f"risk_level={evidence.risk_level!r})"
            ),
            details=evidence.to_dict(),
        ),
        evidence,
    )


def _audit_payload(
    request: AdvisoryEvidenceRetryRequest,
    *,
    evidence: RequiredCodexAdvisoryEvidenceResult,
    pytest_check: PreconditionCheck,
) -> dict[str, Any]:
    return {
        "kind": RETRY_AUDIT_KIND,
        "task_key": request.task_key,
        "from_status": RETRY_FROM_STATUS,
        "to_status": RETRY_TO_STATUS,
        "reason": RETRY_REASON,
        "operator": request.operator,
        "operator_confirmed": True,
        "artifact_dir": str(request.artifact_dir),
        "advisory_requirement_name": evidence.requirement_name,
        "advisory_review_status": evidence.review_status,
        "advisory_risk_level": evidence.risk_level,
        "advisory_json_path": evidence.json_path,
        "pytest_summary_line": pytest_check.details.get("pytest_summary_line"),
        "pytest_log_path": pytest_check.details.get("pytest_log_path"),
        "validator_launch_spec_path": pytest_check.details.get(
            "validator_launch_spec_path"
        ),
        "requires_human_review": True,
        "not_approval": True,
        "not_merge": True,
        "not_cleanup": True,
        "not_validation_authority": True,
        "no_subprocess_invoked": True,
        "no_new_attempt_reserved": True,
    }


def run_advisory_evidence_retry(
    request: AdvisoryEvidenceRetryRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> AdvisoryEvidenceRetryResult:
    """Report on, or perform, one audited advisory-evidence retry transition.

    Without ``confirm_transition`` this is a read-only dry-run report: every
    precondition is evaluated and returned, and nothing is mutated. With
    ``confirm_transition`` and all preconditions satisfied, the same
    ``blocked -> waiting_approval`` transition the runner would have performed
    is applied under a compare-and-set on the ``blocked`` status, and an
    explicit audit event is recorded.

    Unsatisfied preconditions are returned as a structured result with
    ``mutated=False``; they are an operator diagnostic, not an exception.
    ``AdvisoryEvidenceRetryError`` is raised only when the transition itself
    cannot be applied safely (for example the task left ``blocked`` between the
    precondition check and the compare-and-set write).
    """

    current_store = store or TaskMirrorStore(request.db_path)

    status_check, observed_status = _check_task_status(
        current_store, request.task_key
    )
    executor_check = _check_executor_evidence(request.artifact_dir)
    pytest_check = _check_pytest_evidence(request.artifact_dir)
    advisory_check, evidence = _check_advisory_evidence(
        request.artifact_dir, request.task_key
    )

    checks = (status_check, executor_check, pytest_check, advisory_check)
    blocking_errors = tuple(
        check.summary for check in checks if not check.satisfied
    )
    preconditions_satisfied = not blocking_errors

    if not preconditions_satisfied or not request.confirm_transition:
        return AdvisoryEvidenceRetryResult(
            task_key=request.task_key,
            artifact_dir=request.artifact_dir,
            operator=request.operator,
            reason=RETRY_REASON,
            from_status=RETRY_FROM_STATUS,
            to_status=RETRY_TO_STATUS,
            observed_status=observed_status,
            confirm_transition=request.confirm_transition,
            preconditions_satisfied=preconditions_satisfied,
            checks=checks,
            blocking_errors=blocking_errors,
            mutated=False,
            audit_event_recorded=False,
            codex_advisory_evidence=evidence.to_dict(),
        )

    try:
        current_store.update_task_status(
            request.task_key,
            RETRY_TO_STATUS,
            source=RETRY_SOURCE,
            message=(
                "Operator-confirmed advisory evidence retry moved the task to "
                "waiting_approval"
            ),
            expected_current_status=RETRY_FROM_STATUS,
        )
    except (KeyError, ValueError) as exc:
        raise AdvisoryEvidenceRetryError(str(exc)) from exc

    current_store.record_task_event(
        request.task_key,
        RETRY_EVENT_TYPE,
        RETRY_SOURCE,
        message="Operator-confirmed advisory evidence retry transition recorded",
        payload=_audit_payload(
            request,
            evidence=evidence,
            pytest_check=pytest_check,
        ),
    )

    return AdvisoryEvidenceRetryResult(
        task_key=request.task_key,
        artifact_dir=request.artifact_dir,
        operator=request.operator,
        reason=RETRY_REASON,
        from_status=RETRY_FROM_STATUS,
        to_status=RETRY_TO_STATUS,
        observed_status=RETRY_TO_STATUS,
        confirm_transition=True,
        preconditions_satisfied=True,
        checks=checks,
        blocking_errors=(),
        mutated=True,
        audit_event_recorded=True,
        codex_advisory_evidence=evidence.to_dict(),
    )


__all__ = [
    "CHECK_ADVISORY_EVIDENCE",
    "CHECK_EXECUTOR_EVIDENCE",
    "CHECK_PYTEST_EVIDENCE",
    "CHECK_TASK_BLOCKED",
    "PYTEST_LAUNCH_SPEC_FILENAME",
    "PYTEST_LOG_FILENAME",
    "RETRY_AUDIT_KIND",
    "RETRY_EVENT_TYPE",
    "RETRY_FROM_STATUS",
    "RETRY_REASON",
    "RETRY_SOURCE",
    "RETRY_TO_STATUS",
    "AdvisoryEvidenceRetryError",
    "AdvisoryEvidenceRetryRequest",
    "AdvisoryEvidenceRetryResult",
    "PreconditionCheck",
    "run_advisory_evidence_retry",
    "summarize_pytest_log",
]
