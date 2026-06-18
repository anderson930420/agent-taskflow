"""Deterministic Codex advisory artifact contract validator (v0.2.4).

This module adds a deterministic, file-read-only validator that checks whether a
Codex advisory review artifact exists and satisfies the expected artifact
contract established by ``v0.2.1`` / ``v0.2.2`` / ``v0.2.3``.

The validator validates the *artifact contract only*:

    Validate artifact contract, not advisory judgment.

It checks artifact presence, JSON shape, schema/identity fields, task binding,
allowed enums, authority invariants, companion artifacts, structural validity of
``tool_error``, and (since ``v0.2.6``) structured review-checklist coverage. It
never judges the quality, correctness, severity, or usefulness of Codex's
advisory findings.

The ``v0.2.6`` checklist hardening requires every artifact to carry a
``review_checklist`` that covers each required review area (with a status, a
non-empty summary, and a findings list) plus ``human_review_priorities``
guidance. This enforces *checklist coverage, not Codex approval*: a checklist
area reporting ``concern``, ``unknown``, or ``not_applicable`` is valid advisory
evidence and never fails the contract by itself; only a missing or structurally
invalid checklist is a contract failure.

In particular the advisory statuses ``looks_good``, ``needs_attention``,
``high_risk``, and ``tool_error`` are all valid Codex advisory statuses. They are
human-review evidence, not deterministic validator outcomes, so they never cause
this contract validator to fail by themselves.

This module reads files only. It never invokes Codex, never runs a subprocess,
and never approves, blocks, merges, pushes, cleans up, deletes branches or
worktrees, mutates approval records, or changes task lifecycle / scheduler /
runner / ``waiting_approval`` behavior. It is advisory-contract evidence only and
human final approval is always required.

Unlike the ``v0.2.3`` waiting-approval summary (which never fails and downgrades
problems to warnings), this validator has strict pass/fail behavior: a missing
artifact, malformed JSON, or any invariant violation is a validator failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.codex_advisory_review import (
    ALLOWED_CHECKLIST_STATUSES,
    ALLOWED_PRIORITY_AREAS,
    ALLOWED_REVIEW_STATUSES,
    ALLOWED_RISK_LEVELS,
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    REVIEW_CHECKLIST_AREAS,
    REVIEWER,
    SCHEMA_VERSION,
    STDERR_FILENAME,
    STDOUT_FILENAME,
)
from agent_taskflow.tasks import normalize_task_key


VALIDATOR_NAME = "codex_advisory_artifact_contract"


@dataclass(frozen=True)
class CodexAdvisoryArtifactContractValidationRequest:
    """Input for a deterministic Codex advisory artifact contract validation.

    ``task_key`` is optional. When provided, the validator additionally enforces
    that the artifact's ``task_key`` matches the expected task key.
    """

    artifact_dir: Path
    task_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_dir", Path(self.artifact_dir))
        if self.task_key is not None:
            object.__setattr__(self, "task_key", normalize_task_key(self.task_key))


@dataclass(frozen=True)
class CodexAdvisoryArtifactContractValidationResult:
    """Result of validating the Codex advisory artifact contract.

    ``passed`` is the deterministic contract outcome. ``validation_authority`` and
    ``human_review_required`` mirror the artifact's reported invariant fields for
    review display; they do not grant this validator any approval authority.
    """

    validator_name: str
    passed: bool
    artifact_present: bool
    review_status: str | None
    risk_level: str | None
    validation_authority: bool | None
    human_review_required: bool | None
    json_path: str | None
    markdown_path: str | None
    stdout_path: str | None
    stderr_path: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator_name": self.validator_name,
            "passed": self.passed,
            "artifact_present": self.artifact_present,
            "review_status": self.review_status,
            "risk_level": self.risk_level,
            "validation_authority": self.validation_authority,
            "human_review_required": self.human_review_required,
            "json_path": self.json_path,
            "markdown_path": self.markdown_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_codex_advisory_artifact_contract(
    request: CodexAdvisoryArtifactContractValidationRequest,
) -> CodexAdvisoryArtifactContractValidationResult:
    """Validate the Codex advisory artifact contract in ``artifact_dir``.

    Reads files only. Returns a structured result. The validator passes only when
    the artifact exists and satisfies every contract invariant; any missing
    artifact, malformed JSON, or invariant violation fails the validator.
    """

    artifact_dir = Path(request.artifact_dir)
    json_file = artifact_dir / JSON_FILENAME
    markdown_file = artifact_dir / MARKDOWN_FILENAME
    stdout_file = artifact_dir / STDOUT_FILENAME
    stderr_file = artifact_dir / STDERR_FILENAME

    markdown_path = str(markdown_file) if markdown_file.is_file() else None
    stdout_path = str(stdout_file) if stdout_file.is_file() else None
    stderr_path = str(stderr_file) if stderr_file.is_file() else None

    errors: list[str] = []
    warnings: list[str] = []

    # 1. The JSON artifact must exist.
    if not json_file.is_file():
        errors.append(
            f"Codex advisory review artifact {JSON_FILENAME} is missing in "
            f"{artifact_dir}"
        )
        return _result(
            passed=False,
            artifact_present=False,
            review_status=None,
            risk_level=None,
            validation_authority=None,
            human_review_required=None,
            json_path=None,
            markdown_path=markdown_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            errors=errors,
            warnings=warnings,
        )

    json_path = str(json_file)

    # 2. The JSON artifact must parse.
    try:
        raw_text = json_file.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"Codex advisory review JSON could not be parsed: {exc}")
        return _result(
            passed=False,
            artifact_present=True,
            review_status=None,
            risk_level=None,
            validation_authority=None,
            human_review_required=None,
            json_path=json_path,
            markdown_path=markdown_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            errors=errors,
            warnings=warnings,
        )

    # 3. The JSON artifact must be an object.
    if not isinstance(data, dict):
        errors.append("Codex advisory review JSON is not a JSON object")
        return _result(
            passed=False,
            artifact_present=True,
            review_status=None,
            risk_level=None,
            validation_authority=None,
            human_review_required=None,
            json_path=json_path,
            markdown_path=markdown_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            errors=errors,
            warnings=warnings,
        )

    # Best-effort reporting fields (reported as-is, even when invalid).
    review_status = data.get("review_status")
    review_status_report = review_status if isinstance(review_status, str) else None
    risk_level = data.get("risk_level")
    risk_level_report = risk_level if isinstance(risk_level, str) else None
    validation_authority = data.get("validation_authority")
    validation_authority_report = (
        validation_authority if isinstance(validation_authority, bool) else None
    )
    human_review_required = data.get("human_review_required")
    human_review_required_report = (
        human_review_required if isinstance(human_review_required, bool) else None
    )

    # 4. Required schema / identity fields (established by v0.2.1+ contract).
    if "schema_version" not in data:
        errors.append("Codex advisory review JSON is missing required schema_version")
    elif data.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"Codex advisory review schema_version must be {SCHEMA_VERSION!r}, got "
            f"{data.get('schema_version')!r}"
        )
    if "reviewer" not in data:
        errors.append("Codex advisory review JSON is missing required reviewer")
    elif data.get("reviewer") != REVIEWER:
        errors.append(
            f"Codex advisory review reviewer must be {REVIEWER!r}, got "
            f"{data.get('reviewer')!r}"
        )

    # 5. Task identity / binding.
    task_key_value = data.get("task_key")
    if not isinstance(task_key_value, str) or not task_key_value.strip():
        errors.append("Codex advisory review JSON is missing required task_key")
    elif request.task_key is not None:
        try:
            artifact_task_key = normalize_task_key(task_key_value)
        except (ValueError, TypeError):
            artifact_task_key = task_key_value
        if artifact_task_key != request.task_key:
            errors.append(
                f"Codex advisory review task_key {artifact_task_key!r} does not "
                f"match expected task_key {request.task_key!r}"
            )

    # 6. Allowed advisory enums (presence + membership; value itself never fails).
    if "review_status" not in data:
        errors.append("Codex advisory review JSON is missing required review_status")
    elif review_status not in ALLOWED_REVIEW_STATUSES:
        errors.append(
            f"Codex advisory review review_status must be one of "
            f"{ALLOWED_REVIEW_STATUSES}, got {review_status!r}"
        )
    if "risk_level" not in data:
        errors.append("Codex advisory review JSON is missing required risk_level")
    elif risk_level not in ALLOWED_RISK_LEVELS:
        errors.append(
            f"Codex advisory review risk_level must be one of {ALLOWED_RISK_LEVELS}, "
            f"got {risk_level!r}"
        )

    # 7. Authority invariants (always enforced; never overridable by Codex).
    if "validation_authority" not in data:
        errors.append(
            "Codex advisory review JSON is missing required validation_authority"
        )
    elif validation_authority is not False:
        errors.append(
            "Codex advisory review validation_authority must be false; Codex "
            "advisory review is never deterministic validation authority"
        )
    if "human_review_required" not in data:
        errors.append(
            "Codex advisory review JSON is missing required human_review_required"
        )
    elif human_review_required is not True:
        errors.append(
            "Codex advisory review human_review_required must be true; human final "
            "approval is always required"
        )

    # 8. Companion markdown artifact must exist.
    if markdown_path is None:
        errors.append(
            f"Codex advisory review companion artifact {MARKDOWN_FILENAME} is missing"
        )

    # 9. Confirm-run output artifacts must be consistent with the metadata.
    if _expects_codex_outputs(data):
        if stdout_path is None:
            errors.append(
                f"Codex advisory review confirm-run requires {STDOUT_FILENAME} but "
                "the file is missing"
            )
        if stderr_path is None:
            errors.append(
                f"Codex advisory review confirm-run requires {STDERR_FILENAME} but "
                "the file is missing"
            )

    # 10. tool_error must be structurally valid when present / required.
    tool_error = data.get("tool_error")
    if review_status == "tool_error":
        if not _is_valid_tool_error(tool_error):
            errors.append(
                "Codex advisory review review_status is 'tool_error' but tool_error "
                "is missing or structurally invalid (expected an object with "
                "non-empty string 'category' and 'message')"
            )
    elif tool_error is not None and not _is_valid_tool_error(tool_error):
        errors.append(
            "Codex advisory review tool_error is present but structurally invalid "
            "(expected an object with non-empty string 'category' and 'message')"
        )

    # 11. Review checklist coverage (v0.2.6). Every required review area must be
    # present with a valid status, a non-empty summary, and a findings list, and
    # human reviewer priority guidance must be present and well-formed. Checklist
    # *statuses* (concern / unknown / not_applicable) are advisory evidence and
    # never fail the contract by themselves; only a missing or structurally
    # invalid checklist fails the contract.
    _validate_review_checklist(data, errors)
    _validate_human_review_priorities(data, errors)

    # 12. Timestamp is validated when present (established artifact format).
    if "generated_at" in data:
        generated_at = data.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at.strip():
            errors.append(
                "Codex advisory review generated_at must be a non-empty string"
            )
    else:
        warnings.append(
            "Codex advisory review JSON has no generated_at timestamp field"
        )

    return _result(
        passed=not errors,
        artifact_present=True,
        review_status=review_status_report,
        risk_level=risk_level_report,
        validation_authority=validation_authority_report,
        human_review_required=human_review_required_report,
        json_path=json_path,
        markdown_path=markdown_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        errors=errors,
        warnings=warnings,
    )


def _validate_review_checklist(data: dict[str, Any], errors: list[str]) -> None:
    """Validate the v0.2.6 ``review_checklist`` structure.

    Appends a contract error for a missing checklist, a non-object checklist, any
    missing required area, a non-object area, an invalid/missing status, a
    missing/empty summary, or a non-list ``findings``. A valid ``concern`` /
    ``unknown`` / ``not_applicable`` status is never an error.
    """

    if "review_checklist" not in data:
        errors.append(
            "Codex advisory review JSON is missing required review_checklist"
        )
        return
    checklist = data.get("review_checklist")
    if not isinstance(checklist, dict):
        errors.append("Codex advisory review review_checklist must be an object")
        return

    for area in REVIEW_CHECKLIST_AREAS:
        if area not in checklist:
            errors.append(
                f"Codex advisory review review_checklist is missing required area "
                f"{area!r}"
            )
            continue
        entry = checklist.get(area)
        if not isinstance(entry, dict):
            errors.append(
                f"Codex advisory review review_checklist area {area!r} must be an "
                "object"
            )
            continue
        status = entry.get("status")
        if status not in ALLOWED_CHECKLIST_STATUSES:
            errors.append(
                f"Codex advisory review review_checklist area {area!r} status must "
                f"be one of {ALLOWED_CHECKLIST_STATUSES}, got {status!r}"
            )
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            errors.append(
                f"Codex advisory review review_checklist area {area!r} must have a "
                "non-empty summary"
            )
        findings = entry.get("findings")
        if not isinstance(findings, list):
            errors.append(
                f"Codex advisory review review_checklist area {area!r} findings "
                "must be a list"
            )


def _validate_human_review_priorities(
    data: dict[str, Any], errors: list[str]
) -> None:
    """Validate the v0.2.6 ``human_review_priorities`` structure.

    Appends a contract error when the field is missing, is not a list, is empty,
    or any entry is malformed. The contract requires human reviewer priority
    guidance to be *present* (non-empty): an empty list is a contract failure.
    Dry-run and ``tool_error`` artifacts satisfy this with a single fallback
    entry directing the human reviewer to prioritize the review manually.
    """

    if "human_review_priorities" not in data:
        errors.append(
            "Codex advisory review JSON is missing required human_review_priorities"
        )
        return
    priorities = data.get("human_review_priorities")
    if not isinstance(priorities, list):
        errors.append(
            "Codex advisory review human_review_priorities must be a list"
        )
        return
    if not priorities:
        errors.append(
            "Codex advisory review human_review_priorities must be non-empty; "
            "human reviewer priority guidance must be present"
        )
        return

    for index, item in enumerate(priorities):
        if not isinstance(item, dict):
            errors.append(
                f"Codex advisory review human_review_priorities entry {index} must "
                "be an object"
            )
            continue
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority <= 0:
            errors.append(
                f"Codex advisory review human_review_priorities entry {index} must "
                "have a positive integer priority"
            )
        area = item.get("area")
        if area not in ALLOWED_PRIORITY_AREAS:
            errors.append(
                f"Codex advisory review human_review_priorities entry {index} area "
                f"must be one of {ALLOWED_PRIORITY_AREAS}, got {area!r}"
            )
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(
                f"Codex advisory review human_review_priorities entry {index} must "
                "have a non-empty reason"
            )
        suggested_checks = item.get("suggested_checks")
        if not isinstance(suggested_checks, list):
            errors.append(
                f"Codex advisory review human_review_priorities entry {index} "
                "suggested_checks must be a list"
            )


def _expects_codex_outputs(data: dict[str, Any]) -> bool:
    """Return True when the artifact metadata indicates confirm-run output."""

    if bool(data.get("confirm_run")) or bool(data.get("codex_cli_invoked")):
        return True
    artifacts = data.get("artifacts")
    return isinstance(artifacts, dict) and bool(artifacts.get("codex_outputs"))


def _is_valid_tool_error(tool_error: Any) -> bool:
    """Return True when ``tool_error`` matches the established contract shape."""

    if not isinstance(tool_error, dict):
        return False
    category = tool_error.get("category")
    message = tool_error.get("message")
    return (
        isinstance(category, str)
        and bool(category.strip())
        and isinstance(message, str)
        and bool(message.strip())
    )


def _result(
    *,
    passed: bool,
    artifact_present: bool,
    review_status: str | None,
    risk_level: str | None,
    validation_authority: bool | None,
    human_review_required: bool | None,
    json_path: str | None,
    markdown_path: str | None,
    stdout_path: str | None,
    stderr_path: str | None,
    errors: list[str],
    warnings: list[str],
) -> CodexAdvisoryArtifactContractValidationResult:
    return CodexAdvisoryArtifactContractValidationResult(
        validator_name=VALIDATOR_NAME,
        passed=passed,
        artifact_present=artifact_present,
        review_status=review_status,
        risk_level=risk_level,
        validation_authority=validation_authority,
        human_review_required=human_review_required,
        json_path=json_path,
        markdown_path=markdown_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


__all__ = [
    "VALIDATOR_NAME",
    "CodexAdvisoryArtifactContractValidationRequest",
    "CodexAdvisoryArtifactContractValidationResult",
    "validate_codex_advisory_artifact_contract",
]
