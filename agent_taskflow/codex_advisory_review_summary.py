"""Read-only Codex advisory review artifact summary for human review.

This module detects Codex advisory review artifacts that may exist in a task
artifact directory and summarizes them as human-review evidence only. It reads
files only.

It never invokes Codex, never runs a subprocess, never validates, approves,
blocks, merges, pushes, cleans up, deletes branches/worktrees, or changes task
lifecycle. Codex advisory review is never deterministic validation authority and
human review is always required. The two hard invariants
``validation_authority = false`` and ``human_review_required = true`` are always
forced by this summary regardless of what the artifact JSON claims.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.codex_advisory_review import (
    ALLOWED_REVIEW_STATUSES,
    ALLOWED_RISK_LEVELS,
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    STDERR_FILENAME,
    STDOUT_FILENAME,
)

# Summary-level review_status values that are not Codex-produced findings but
# describe how the artifact itself was read.
MISSING_REVIEW_STATUS = "missing"
MALFORMED_REVIEW_STATUS = "malformed"
UNKNOWN_REVIEW_STATUS = "unknown"
UNKNOWN_RISK_LEVEL = "unknown"


@dataclass(frozen=True)
class CodexAdvisoryReviewSummary:
    """Read-only summary of Codex advisory review artifacts for human review.

    This is advisory evidence only. ``validation_authority`` is always false and
    ``human_review_required`` is always true; neither can be overridden by the
    artifact JSON.
    """

    present: bool
    review_status: str
    risk_level: str
    validation_authority: bool
    human_review_required: bool
    json_path: str | None
    markdown_path: str | None
    stdout_path: str | None
    stderr_path: str | None
    summary: str
    tool_error: dict[str, Any] | None
    review_checklist: dict[str, Any] | None
    human_review_priorities: list[dict[str, Any]]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "review_status": self.review_status,
            "risk_level": self.risk_level,
            "validation_authority": self.validation_authority,
            "human_review_required": self.human_review_required,
            "json_path": self.json_path,
            "markdown_path": self.markdown_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "summary": self.summary,
            "tool_error": self.tool_error,
            "review_checklist": self.review_checklist,
            "human_review_priorities": list(self.human_review_priorities),
            "warnings": list(self.warnings),
        }


def summarize_codex_advisory_review_artifacts(
    artifact_dir: Path | None,
) -> CodexAdvisoryReviewSummary:
    """Summarize any Codex advisory review artifacts in ``artifact_dir``.

    Reads files only. Never invokes Codex, never runs a subprocess, and never
    changes task lifecycle. Always returns a summary; it never raises and never
    fails the calling waiting-approval summary, even when the artifact JSON is
    missing, malformed, inconsistent, or invariant-violating.
    """

    if artifact_dir is None:
        return _absent_summary()

    artifact_dir = Path(artifact_dir)
    json_file = artifact_dir / JSON_FILENAME
    markdown_file = artifact_dir / MARKDOWN_FILENAME
    stdout_file = artifact_dir / STDOUT_FILENAME
    stderr_file = artifact_dir / STDERR_FILENAME

    markdown_path = str(markdown_file) if markdown_file.is_file() else None
    stdout_path = str(stdout_file) if stdout_file.is_file() else None
    stderr_path = str(stderr_file) if stderr_file.is_file() else None

    if not json_file.is_file():
        return _absent_summary(
            markdown_path=markdown_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    json_path = str(json_file)

    try:
        raw_text = json_file.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _malformed_summary(
            json_path=json_path,
            markdown_path=markdown_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            warning=f"Codex advisory review JSON could not be parsed: {exc}",
        )

    if not isinstance(data, dict):
        return _malformed_summary(
            json_path=json_path,
            markdown_path=markdown_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            warning="Codex advisory review JSON is not a JSON object",
        )

    warnings: list[str] = []

    # Hard invariants: never trust the artifact's claimed authority. We always
    # force validation_authority=false and human_review_required=true.
    if "validation_authority" in data and data.get("validation_authority") is not False:
        warnings.append(
            "Codex advisory review JSON claimed validation_authority is not false; "
            "forcing validation_authority=false (Codex advisory review is never "
            "deterministic validation authority)"
        )
    if "human_review_required" in data and data.get("human_review_required") is not True:
        warnings.append(
            "Codex advisory review JSON claimed human_review_required is not true; "
            "forcing human_review_required=true (human review is always required)"
        )

    review_status_raw = data.get("review_status")
    if review_status_raw in ALLOWED_REVIEW_STATUSES:
        review_status = review_status_raw
    else:
        review_status = UNKNOWN_REVIEW_STATUS
        warnings.append(
            f"Codex advisory review JSON has invalid review_status "
            f"{review_status_raw!r}; using 'unknown'"
        )

    risk_level_raw = data.get("risk_level")
    if risk_level_raw in ALLOWED_RISK_LEVELS:
        risk_level = risk_level_raw
    else:
        risk_level = UNKNOWN_RISK_LEVEL
        warnings.append(
            f"Codex advisory review JSON has invalid risk_level "
            f"{risk_level_raw!r}; using 'unknown'"
        )

    summary_text = data.get("summary")
    if not isinstance(summary_text, str):
        summary_text = ""

    tool_error = data.get("tool_error")
    if not isinstance(tool_error, dict):
        tool_error = None

    # v0.2.6 review checklist coverage and human reviewer priority guidance are
    # surfaced as human-review evidence only. They are reported as-is; this
    # lenient summary never fails on a missing or malformed checklist.
    review_checklist = data.get("review_checklist")
    if not isinstance(review_checklist, dict):
        review_checklist = None
    human_review_priorities_raw = data.get("human_review_priorities")
    if isinstance(human_review_priorities_raw, list):
        human_review_priorities = [
            item for item in human_review_priorities_raw if isinstance(item, dict)
        ]
    else:
        human_review_priorities = []

    # Companion artifact consistency. The markdown summary is always generated
    # alongside the JSON; stdout/stderr exist only for confirm-run output.
    if markdown_path is None:
        warnings.append(
            f"Codex advisory review references {MARKDOWN_FILENAME} but the file is "
            "missing"
        )
    if _expects_codex_outputs(data):
        if stdout_path is None:
            warnings.append(
                f"Codex advisory review confirm-run references {STDOUT_FILENAME} "
                "but the file is missing"
            )
        if stderr_path is None:
            warnings.append(
                f"Codex advisory review confirm-run references {STDERR_FILENAME} "
                "but the file is missing"
            )

    return CodexAdvisoryReviewSummary(
        present=True,
        review_status=review_status,
        risk_level=risk_level,
        validation_authority=False,
        human_review_required=True,
        json_path=json_path,
        markdown_path=markdown_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary=summary_text,
        tool_error=tool_error,
        review_checklist=review_checklist,
        human_review_priorities=human_review_priorities,
        warnings=tuple(warnings),
    )


def _expects_codex_outputs(data: dict[str, Any]) -> bool:
    if bool(data.get("confirm_run")) or bool(data.get("codex_cli_invoked")):
        return True
    artifacts = data.get("artifacts")
    return isinstance(artifacts, dict) and bool(artifacts.get("codex_outputs"))


def _absent_summary(
    *,
    markdown_path: str | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> CodexAdvisoryReviewSummary:
    return CodexAdvisoryReviewSummary(
        present=False,
        review_status=MISSING_REVIEW_STATUS,
        risk_level=UNKNOWN_RISK_LEVEL,
        validation_authority=False,
        human_review_required=True,
        json_path=None,
        markdown_path=markdown_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary="",
        tool_error=None,
        review_checklist=None,
        human_review_priorities=[],
        warnings=(),
    )


def _malformed_summary(
    *,
    json_path: str,
    markdown_path: str | None,
    stdout_path: str | None,
    stderr_path: str | None,
    warning: str,
) -> CodexAdvisoryReviewSummary:
    return CodexAdvisoryReviewSummary(
        present=True,
        review_status=MALFORMED_REVIEW_STATUS,
        risk_level=UNKNOWN_RISK_LEVEL,
        validation_authority=False,
        human_review_required=True,
        json_path=json_path,
        markdown_path=markdown_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary="",
        tool_error=None,
        review_checklist=None,
        human_review_priorities=[],
        warnings=(warning,),
    )


__all__ = [
    "CodexAdvisoryReviewSummary",
    "MALFORMED_REVIEW_STATUS",
    "MISSING_REVIEW_STATUS",
    "UNKNOWN_REVIEW_STATUS",
    "UNKNOWN_RISK_LEVEL",
    "summarize_codex_advisory_review_artifacts",
]
