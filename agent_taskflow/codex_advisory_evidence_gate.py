"""Required Codex advisory artifact evidence gate (v0.2.5).

This module adds a small, explicit pre-``waiting_approval`` evidence gate. It
requires that a valid Codex advisory artifact contract (the ``v0.2.4`` validator
``codex_advisory_artifact_contract``) is present *before* a task may transition
into ``waiting_approval``.

The core semantic is::

    Require Codex advisory evidence, not Codex approval.

The gate is satisfied when the deterministic Codex advisory artifact contract
validator passes. It is *not* satisfied when the artifact is missing, malformed,
not an object, bound to the wrong task, missing required schema/identity fields,
missing required companion artifacts, has an invalid ``review_status`` /
``risk_level``, violates the authority invariants (``validation_authority`` must
be ``false`` and ``human_review_required`` must be ``true``), or carries a
structurally invalid ``tool_error``.

The gate must never fail merely because Codex reported ``looks_good``,
``needs_attention``, ``high_risk``, or ``tool_error``; those are all valid
advisory statuses and are human-review evidence, not gate decisions. A
structurally valid ``tool_error`` artifact is still valid required evidence.

This module reads files only (through the contract validator). It never invokes
Codex, never runs a subprocess, never pushes branches, creates PRs, merges,
cleans up, deletes branches/worktrees, mutates approval records, or grants Codex
any validation/approval authority. It only reports whether the required advisory
*contract evidence* exists so the runner can decide whether the deterministic
transition into ``waiting_approval`` is allowed. Human final approval is always
required.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.codex_advisory_artifact_contract_validator import (
    CodexAdvisoryArtifactContractValidationRequest,
    validate_codex_advisory_artifact_contract,
)
from agent_taskflow.tasks import normalize_task_key


REQUIREMENT_NAME = "codex_advisory_artifact_evidence"


@dataclass(frozen=True)
class RequiredCodexAdvisoryEvidenceRequest:
    """Input for the required Codex advisory evidence gate.

    ``artifact_dir`` is the task artifact directory that should already contain a
    Codex advisory review artifact. ``task_key`` binds the evidence to a specific
    task so a mismatched artifact does not satisfy the requirement.
    """

    artifact_dir: Path
    task_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_dir", Path(self.artifact_dir))
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))


@dataclass(frozen=True)
class RequiredCodexAdvisoryEvidenceResult:
    """Result of checking the required Codex advisory evidence gate.

    ``satisfied`` is the deterministic gate outcome (the contract validator
    passed). ``review_status`` / ``risk_level`` mirror the artifact's reported
    advisory fields for review display only; they never affect ``satisfied``.
    ``blocking_errors`` carries the contract validator errors verbatim so the
    runner can surface them as required-evidence blockers.
    """

    requirement_name: str
    satisfied: bool
    validator_name: str
    artifact_present: bool
    review_status: str | None
    risk_level: str | None
    json_path: str | None
    markdown_path: str | None
    stdout_path: str | None
    stderr_path: str | None
    blocking_errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_name": self.requirement_name,
            "satisfied": self.satisfied,
            "validator_name": self.validator_name,
            "artifact_present": self.artifact_present,
            "review_status": self.review_status,
            "risk_level": self.risk_level,
            "json_path": self.json_path,
            "markdown_path": self.markdown_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "blocking_errors": list(self.blocking_errors),
            "warnings": list(self.warnings),
        }

    def blocking_summary(self) -> str:
        """Return a human-readable, evidence-framed blocking summary."""

        detail = "; ".join(self.blocking_errors) if self.blocking_errors else (
            "Codex advisory artifact contract did not pass"
        )
        return (
            "Codex advisory artifact evidence is required before waiting_approval: "
            f"{detail}"
        )


def check_required_codex_advisory_evidence(
    request: RequiredCodexAdvisoryEvidenceRequest,
) -> RequiredCodexAdvisoryEvidenceResult:
    """Check whether required Codex advisory artifact evidence is present.

    Delegates to the deterministic ``codex_advisory_artifact_contract`` validator
    and reframes its strict pass/fail result as a required-evidence gate result.
    Reads files only; never invokes Codex, runs a subprocess, or mutates state.

    A missing, malformed, or contract-invalid artifact yields
    ``satisfied=False`` with the validator errors surfaced as ``blocking_errors``.
    A valid contract yields ``satisfied=True`` regardless of whether the advisory
    ``review_status`` is ``looks_good``, ``needs_attention``, ``high_risk``, or a
    structurally valid ``tool_error``.
    """

    validation = validate_codex_advisory_artifact_contract(
        CodexAdvisoryArtifactContractValidationRequest(
            artifact_dir=request.artifact_dir,
            task_key=request.task_key,
        )
    )

    return RequiredCodexAdvisoryEvidenceResult(
        requirement_name=REQUIREMENT_NAME,
        satisfied=validation.passed,
        validator_name=validation.validator_name,
        artifact_present=validation.artifact_present,
        review_status=validation.review_status,
        risk_level=validation.risk_level,
        json_path=validation.json_path,
        markdown_path=validation.markdown_path,
        stdout_path=validation.stdout_path,
        stderr_path=validation.stderr_path,
        blocking_errors=validation.errors,
        warnings=validation.warnings,
    )


__all__ = [
    "REQUIREMENT_NAME",
    "RequiredCodexAdvisoryEvidenceRequest",
    "RequiredCodexAdvisoryEvidenceResult",
    "check_required_codex_advisory_evidence",
]
