"""Codex advisory reviewer contract.

This module builds a Codex advisory review contract over an existing task
artifact directory. It inspects evidence file presence, renders a Codex CLI
review prompt, and generates review artifacts for the Codex CLI design/code
review stage.

The default mode (since v0.2.1) is dry-run: it does not invoke the Codex CLI or
any subprocess. Since v0.2.2 the reviewer also supports an explicit opt-in
confirm-run mode (``confirm_run=True``) that invokes the Codex CLI exactly once,
captures stdout/stderr/exit-code/timeout/duration, and parses Codex output into
advisory findings only.

In every mode the Codex advisory reviewer is advisory only: it is never
deterministic validation authority, and it never approves, blocks, merges,
pushes, cleans up, deletes branches/worktrees, or changes task lifecycle. The
two hard invariants ``validation_authority = false`` and
``human_review_required = true`` are always enforced by agent-taskflow and can
never be overridden by Codex output.

Deterministic validators remain pytest / compileall / policy / changed-files.
Human final approval is always required.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.atomic_write import atomic_write_json, atomic_write_text
from agent_taskflow.models import utc_now_iso
from agent_taskflow.tasks import normalize_task_key


SCHEMA_VERSION = "codex_advisory_review.v1"
REVIEWER = "codex-cli"
SOURCE = "codex_advisory_review"

PROMPT_FILENAME = "codex-advisory-review-prompt.md"
JSON_FILENAME = "codex-advisory-review.json"
MARKDOWN_FILENAME = "codex-advisory-review.md"
STDOUT_FILENAME = "codex-advisory-review-stdout.txt"
STDERR_FILENAME = "codex-advisory-review-stderr.txt"
GENERATED_ARTIFACT_FILENAMES = (
    PROMPT_FILENAME,
    JSON_FILENAME,
    MARKDOWN_FILENAME,
)

DEFAULT_CODEX_COMMAND = "codex"
DEFAULT_TIMEOUT_SECONDS = 300

# Mutable advisory fields Codex output may contribute. Everything else (schema,
# reviewer, task_key, validation_authority, human_review_required, artifacts,
# generated_at, paths, governance) is canonical and Codex can never override it.
# ``review_checklist`` and ``human_review_priorities`` are also Codex-contributed
# but are merged/validated specially (see ``merge_codex_findings``) rather than
# through the generic mutable-field copy.
CODEX_MUTABLE_FIELDS = (
    "review_status",
    "summary",
    "design_findings",
    "correctness_findings",
    "test_coverage_findings",
    "architecture_boundary_findings",
    "risk_level",
    "recommended_human_focus",
    "suggested_followups",
    "missing_evidence",
)
_CODEX_LIST_FIELDS = (
    "design_findings",
    "correctness_findings",
    "test_coverage_findings",
    "architecture_boundary_findings",
    "recommended_human_focus",
    "suggested_followups",
    "missing_evidence",
)
_FENCED_BLOCK_RE = re.compile(r"```(?:[A-Za-z0-9_+-]+)?[ \t]*\n(.*?)```", re.DOTALL)

ALLOWED_REVIEW_STATUSES = (
    "not_run",
    "looks_good",
    "needs_attention",
    "high_risk",
    "tool_error",
)
ALLOWED_RISK_LEVELS = (
    "unknown",
    "low",
    "medium",
    "high",
)

# v0.2.6 — Codex advisory review checklist hardening.
#
# Every Codex advisory artifact must carry a structured review checklist that
# explicitly covers each of the review areas a human reviewer cares about, plus
# explicit human reviewer priority guidance. This is *checklist coverage, not
# Codex approval*: the checklist statuses are advisory evidence only and never
# grant Codex validator/approval authority. A ``concern`` / ``unknown`` /
# ``not_applicable`` status never blocks by itself; only a missing or
# structurally invalid checklist makes the artifact contract-invalid.
REVIEW_CHECKLIST_AREAS = (
    "architecture_boundary",
    "design_risk",
    "test_quality",
    "silent_failure",
    "fallback_correctness",
    "race_concurrency",
    "path_cwd_repo_root",
    "human_review_priority",
)
ALLOWED_CHECKLIST_STATUSES = (
    "pass",
    "concern",
    "not_applicable",
    "unknown",
)
# Human reviewer priority entries may reference any checklist area or "other".
ALLOWED_PRIORITY_AREAS = REVIEW_CHECKLIST_AREAS + ("other",)

DRY_RUN_CHECKLIST_STATUS = "unknown"
DRY_RUN_CHECKLIST_SUMMARY = (
    "Not assessed in dry-run mode; Codex CLI was not invoked, so no advisory "
    "checklist finding was produced for this area."
)
CONFIRM_PENDING_CHECKLIST_SUMMARY = (
    "Codex advisory review did not report a finding for this area; treat as "
    "unknown and review manually."
)
TOOL_ERROR_CHECKLIST_STATUS = "unknown"
TOOL_ERROR_CHECKLIST_SUMMARY = (
    "Not assessed; the Codex advisory review failed or timed out before "
    "producing a checklist finding for this area."
)

# Human reviewer priority guidance must always be *present* (non-empty), not just
# a present field. When Codex produced no priority guidance (dry-run, confirm-run
# with no priorities, or tool_error) the artifact still carries a single fallback
# priority entry directing the human reviewer to prioritize the review manually.
DRY_RUN_PRIORITY_REASON = (
    "Dry-run advisory review produced no Codex priority guidance; a human "
    "reviewer must independently prioritize every required review area."
)
CONFIRM_PENDING_PRIORITY_REASON = (
    "Codex advisory review did not report priority guidance; a human reviewer "
    "must independently prioritize every required review area."
)
TOOL_ERROR_PRIORITY_REASON = (
    "Codex advisory review failed or timed out before producing priority "
    "guidance; a human reviewer must independently prioritize every required "
    "review area."
)
DEFAULT_PRIORITY_SUGGESTED_CHECKS = (
    "Independently review every checklist area listed in review_checklist",
    "Decide review priority manually; no Codex priority guidance is available",
)

DRY_RUN_REVIEW_STATUS = "not_run"
DRY_RUN_RISK_LEVEL = "unknown"

TOOL_ERROR_REVIEW_STATUS = "tool_error"
TOOL_ERROR_RISK_LEVEL = "unknown"

# Advisory review dimensions a future Codex CLI reviewer should cover.
REVIEW_DIMENSIONS = (
    "Task fit",
    "Architecture fit",
    "Minimality",
    "Correctness risk",
    "Test adequacy",
    "Failure behavior",
    "Security / governance",
    "Human review focus",
)

# Executor-neutral evidence detection. These names are common across executor
# backends (manual, shell, opencode, pi) and are expected to work naturally for
# a future Claude Code executor too. Detection is generic file-presence only.
KNOWN_EVIDENCE_FILES = (
    "task_execution_package.json",
    "implementation_prompt.md",
    "mission_contract.json",
    "pytest.log",
    "compileall.log",
    "policy-validate.log",
    "changed-files-audit.json",
)
# Generic, executor-neutral log discovery. Do not hard-code opencode/pi/shell.
EXECUTOR_LOG_GLOB = "*.log"

# Governance prohibitions surfaced in every generated artifact.
GOVERNANCE_PROHIBITIONS = (
    "no approve",
    "no block",
    "no merge",
    "no push",
    "no cleanup",
    "no delete branch",
    "no delete worktree",
    "no lifecycle change",
)


class CodexAdvisoryReviewError(RuntimeError):
    """Raised when a Codex advisory review payload or request is invalid."""


@dataclass(frozen=True)
class CodexAdvisoryReviewRequest:
    """Request for generating a Codex advisory review.

    ``dry_run`` is the default and does not invoke any subprocess. ``confirm_run``
    is the explicit opt-in that invokes the Codex CLI exactly once. The two modes
    are mutually exclusive; ``codex_command`` and ``timeout_seconds`` only apply
    when ``confirm_run`` is set.
    """

    task_key: str
    artifact_dir: Path
    repo_path: Path | None = None
    worktree_path: Path | None = None
    dry_run: bool = True
    confirm_run: bool = False
    codex_command: str = DEFAULT_CODEX_COMMAND
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.confirm_run and self.dry_run:
            raise ValueError(
                "dry_run and confirm_run are mutually exclusive; confirm-run "
                "mode must be requested with confirm_run=True and dry_run=False"
            )
        if not self.confirm_run and not self.dry_run:
            raise ValueError(
                "dry_run must be true unless confirm_run is explicitly set; "
                "Codex CLI is only invoked in confirm-run mode"
            )
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, int
        ):
            raise ValueError("timeout_seconds must be an integer")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        command = (self.codex_command or "").strip()
        object.__setattr__(self, "codex_command", command)
        if self.confirm_run and not shlex.split(command):
            raise ValueError("codex_command must not be empty in confirm-run mode")
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "artifact_dir",
            _normalize_path(self.artifact_dir, name="artifact_dir"),
        )
        if self.repo_path is not None:
            object.__setattr__(
                self,
                "repo_path",
                _normalize_path(self.repo_path, name="repo_path"),
            )
        if self.worktree_path is not None:
            object.__setattr__(
                self,
                "worktree_path",
                _normalize_path(self.worktree_path, name="worktree_path"),
            )


@dataclass(frozen=True)
class CodexAdvisoryReviewResult:
    """Result of generating dry-run Codex advisory review artifacts."""

    task_key: str
    artifact_dir: Path
    prompt_path: Path
    json_path: Path
    markdown_path: Path
    payload: dict[str, Any]
    evidence: dict[str, Any]
    dry_run: bool
    confirm_run: bool = False
    stdout_path: Path | None = None
    stderr_path: Path | None = None

    def artifact_paths(self) -> list[Path]:
        return [self.prompt_path, self.json_path, self.markdown_path]

    def codex_output_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self.stdout_path is not None:
            paths.append(self.stdout_path)
        if self.stderr_path is not None:
            paths.append(self.stderr_path)
        return paths


def _normalize_path(path: str | Path, *, name: str) -> Path:
    if path is None:
        raise ValueError(f"{name} must not be None")
    return Path(path).expanduser().resolve()


def build_default_checklist(*, status: str, summary: str) -> dict[str, dict[str, Any]]:
    """Build a structurally valid review checklist covering every required area.

    Every required area gets ``status``, a non-empty ``summary``, and an empty
    ``findings`` list. This is the fallback checklist used by dry-run and
    ``tool_error`` artifacts (where Codex never produced a real review) so that
    every artifact still deterministically expresses coverage of every required
    review area.
    """

    return {
        area: {"status": status, "summary": summary, "findings": []}
        for area in REVIEW_CHECKLIST_AREAS
    }


def build_default_human_review_priorities(*, reason: str) -> list[dict[str, Any]]:
    """Build a structurally valid, non-empty human reviewer priority list.

    Used as the fallback when Codex produced no priority guidance (dry-run,
    confirm-run that omitted priorities, or ``tool_error``). The v0.2.6 contract
    requires the guidance to be *present* (non-empty), so this always returns at
    least one entry directing the human reviewer to prioritize the review.
    """

    return [
        {
            "priority": 1,
            "area": "human_review_priority",
            "reason": reason,
            "suggested_checks": list(DEFAULT_PRIORITY_SUGGESTED_CHECKS),
        }
    ]


def detect_evidence(artifact_dir: Path) -> dict[str, Any]:
    """Inspect file presence in ``artifact_dir`` and build an evidence manifest.

    Detection is read-only file-presence inspection. It does not read file
    contents, run subprocesses, or invoke any executor or reviewer.
    """

    artifact_dir = Path(artifact_dir)
    files: list[dict[str, Any]] = []
    present_names: list[str] = []
    missing_names: list[str] = []
    for name in KNOWN_EVIDENCE_FILES:
        candidate = artifact_dir / name
        present = candidate.is_file()
        files.append(
            {
                "name": name,
                "present": present,
                "path": str(candidate) if present else None,
            }
        )
        if present:
            present_names.append(name)
        else:
            missing_names.append(name)

    discovered_logs: list[dict[str, Any]] = []
    if artifact_dir.is_dir():
        known = set(KNOWN_EVIDENCE_FILES)
        for log_path in sorted(artifact_dir.glob(EXECUTOR_LOG_GLOB)):
            if not log_path.is_file():
                continue
            if log_path.name in known:
                continue
            discovered_logs.append(
                {
                    "name": log_path.name,
                    "present": True,
                    "path": str(log_path),
                }
            )

    return {
        "artifact_dir": str(artifact_dir),
        "artifact_dir_exists": artifact_dir.is_dir(),
        "files": files,
        "executor_logs": discovered_logs,
        "present_evidence": present_names,
        "missing_evidence": missing_names,
        "evidence_present_count": len(present_names) + len(discovered_logs),
    }


def build_review_prompt(
    request: CodexAdvisoryReviewRequest,
    evidence: dict[str, Any],
) -> str:
    """Render the Codex CLI advisory review prompt."""

    dimension_lines = "\n".join(f"- {dimension}" for dimension in REVIEW_DIMENSIONS)
    checklist_lines = "\n".join(f"- {area}" for area in REVIEW_CHECKLIST_AREAS)
    checklist_statuses = ", ".join(ALLOWED_CHECKLIST_STATUSES)
    priority_areas = ", ".join(ALLOWED_PRIORITY_AREAS)
    evidence_lines = "\n".join(
        f"- {item['name']}: {'present' if item['present'] else 'missing'}"
        for item in evidence["files"]
    )
    log_lines = (
        "\n".join(f"- {item['name']}: present" for item in evidence["executor_logs"])
        if evidence["executor_logs"]
        else "- (no additional executor logs detected)"
    )

    return f"""# Codex Advisory Review Prompt

Task key: {request.task_key}
Artifact directory: {request.artifact_dir}
Repo path: {request.repo_path if request.repo_path is not None else '(not provided)'}
Worktree path: {request.worktree_path if request.worktree_path is not None else '(not provided)'}

## Role

You are Codex CLI acting as an advisory design/code reviewer for an
agent-taskflow task that has already been implemented and validated by
deterministic validators. Review the evidence in the artifact directory and
provide advisory findings for a human reviewer.

## What to Review

Review each of the following dimensions:

{dimension_lines}

## Evidence to Read (if present)

The following common evidence files may exist in the artifact directory. Read
whatever is present. Evidence detection is generic and executor-neutral; it must
work for any executor backend (manual, shell, opencode, pi, and a future Claude
Code executor):

- task_execution_package.json
- implementation_prompt.md
- mission_contract.json
- executor logs
- pytest.log
- compileall.log
- policy-validate.log
- changed-files-audit.json

Detected evidence in this artifact directory:

{evidence_lines}

Detected executor logs:

{log_lines}

## Authority Boundaries (read carefully)

- Codex is an advisory reviewer only.
- Codex is not deterministic validation authority.
- Codex must not approve, block, merge, push, or cleanup.
- Codex must not delete branches or delete worktrees.
- Codex must not change lifecycle.
- Human final approval is required.
- Deterministic validators remain pytest / compileall / policy / changed-files.

Codex advisory review output is never treated as deterministic validation. It
never approves, blocks, merges, pushes, cleans up, deletes branches, deletes
worktrees, or changes task lifecycle state. The human reviewer remains the final
gate.

## Output Contract

Produce findings that map to the JSON contract `{SCHEMA_VERSION}` with
`validation_authority` always false and `human_review_required` always true.
Allowed review_status values: {', '.join(ALLOWED_REVIEW_STATUSES)}.
Allowed risk_level values: {', '.join(ALLOWED_RISK_LEVELS)}.

Return a JSON object with advisory findings. Do not claim validation authority.
Do not claim approval authority. Do not use pass/fail/approved/rejected/blocked
semantics. You may return any of the following advisory fields; omit any you
cannot assess:

- review_status
- summary
- design_findings
- correctness_findings
- test_coverage_findings
- architecture_boundary_findings
- risk_level
- recommended_human_focus
- suggested_followups
- missing_evidence

## Required Review Checklist (v0.2.6)

You must also return a `review_checklist` object that explicitly covers every
one of the following review areas, so a human reviewer can see that each area was
considered:

{checklist_lines}

Each checklist area must be an object with:

- `status`: one of {checklist_statuses}
- `summary`: a non-empty string describing what you found for that area
- `findings`: a list (possibly empty) of specific advisory finding strings

`concern`, `unknown`, and `not_applicable` are advisory statuses, not blockers.
They report coverage; they never approve, block, or validate. Use `unknown` when
you could not assess an area rather than omitting it.

Also return a `human_review_priorities` list ordered by importance. Each entry is
an object with:

- `priority`: an integer (1 is highest priority)
- `area`: one of {priority_areas}
- `reason`: a non-empty string explaining why a human should look here
- `suggested_checks`: a list of concrete things a human reviewer should check

You may return the JSON object directly, or inside a fenced ```json block. Do
not set `validation_authority`, `human_review_required`, `schema_version`,
`reviewer`, `task_key`, `artifacts`, `generated_at`, or `governance`; those are
owned by agent-taskflow and will be enforced regardless of your output.
"""


def build_review_payload(
    request: CodexAdvisoryReviewRequest,
    evidence: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the dry-run Codex advisory review JSON payload."""

    generated_at = generated_at or utc_now_iso()
    artifacts = {
        "generated": {
            name: str(request.artifact_dir / name)
            for name in GENERATED_ARTIFACT_FILENAMES
        },
        "evidence": {
            item["name"]: item["path"]
            for item in evidence["files"]
            if item["present"]
        },
        "executor_logs": {
            item["name"]: item["path"] for item in evidence["executor_logs"]
        },
    }

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "reviewer": REVIEWER,
        "source": SOURCE,
        "task_key": request.task_key,
        "review_status": DRY_RUN_REVIEW_STATUS,
        "validation_authority": False,
        "human_review_required": True,
        "summary": "",
        "design_findings": [],
        "correctness_findings": [],
        "test_coverage_findings": [],
        "architecture_boundary_findings": [],
        "risk_level": DRY_RUN_RISK_LEVEL,
        "recommended_human_focus": [],
        "suggested_followups": [],
        "missing_evidence": list(evidence["missing_evidence"]),
        "review_checklist": build_default_checklist(
            status=DRY_RUN_CHECKLIST_STATUS,
            summary=(
                DRY_RUN_CHECKLIST_SUMMARY
                if request.dry_run
                else CONFIRM_PENDING_CHECKLIST_SUMMARY
            ),
        ),
        "human_review_priorities": build_default_human_review_priorities(
            reason=(
                DRY_RUN_PRIORITY_REASON
                if request.dry_run
                else CONFIRM_PENDING_PRIORITY_REASON
            ),
        ),
        "artifacts": artifacts,
        "dry_run": bool(request.dry_run),
        "confirm_run": bool(request.confirm_run),
        "codex_cli_invoked": False,
        "subprocess_invoked": False,
        "codex_invocation": None,
        "tool_error": None,
        "generated_at": generated_at,
        "repo_path": str(request.repo_path) if request.repo_path is not None else None,
        "worktree_path": (
            str(request.worktree_path) if request.worktree_path is not None else None
        ),
        "artifact_dir": str(request.artifact_dir),
        "review_dimensions": list(REVIEW_DIMENSIONS),
        "evidence": evidence,
        "governance": {
            "advisory_only": True,
            "deterministic_validation_authority": False,
            "human_review_required": True,
            "deterministic_validators": [
                "pytest",
                "compileall",
                "policy",
                "changed-files",
            ],
            "prohibitions": list(GOVERNANCE_PROHIBITIONS),
            "codex_cli_invoked": False,
            "subprocess_invoked": False,
        },
    }
    return payload


def validate_payload(payload: dict[str, Any]) -> None:
    """Validate hard invariants before writing a payload.

    Raises ``CodexAdvisoryReviewError`` if any invariant is violated.
    """

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CodexAdvisoryReviewError(
            f"schema_version must be {SCHEMA_VERSION!r}, got "
            f"{payload.get('schema_version')!r}"
        )
    if payload.get("reviewer") != REVIEWER:
        raise CodexAdvisoryReviewError(
            f"reviewer must be {REVIEWER!r}, got {payload.get('reviewer')!r}"
        )
    if payload.get("validation_authority") is not False:
        raise CodexAdvisoryReviewError(
            "validation_authority must always be false; Codex advisory review is "
            "never deterministic validation authority"
        )
    if payload.get("human_review_required") is not True:
        raise CodexAdvisoryReviewError(
            "human_review_required must always be true; human final approval is "
            "required"
        )
    review_status = payload.get("review_status")
    if review_status not in ALLOWED_REVIEW_STATUSES:
        raise CodexAdvisoryReviewError(
            f"review_status must be one of {ALLOWED_REVIEW_STATUSES}, got "
            f"{review_status!r}"
        )
    risk_level = payload.get("risk_level")
    if risk_level not in ALLOWED_RISK_LEVELS:
        raise CodexAdvisoryReviewError(
            f"risk_level must be one of {ALLOWED_RISK_LEVELS}, got {risk_level!r}"
        )


def build_review_markdown(payload: dict[str, Any]) -> str:
    """Render a human-readable review summary for dry-run or confirm-run."""

    evidence = payload.get("evidence", {})
    present = evidence.get("present_evidence", [])
    missing = payload.get("missing_evidence", [])
    logs = evidence.get("executor_logs", [])

    dry_run = bool(payload.get("dry_run", True))
    codex_invoked = bool(payload.get("codex_cli_invoked", False))
    mode = "Dry Run" if dry_run else "Confirm Run"
    review_status = payload["review_status"]

    lines = [
        f"# Codex Advisory Review ({mode})",
        "",
        f"- Task key: {payload['task_key']}",
        f"- Reviewer: {payload['reviewer']}",
        f"- Schema version: {payload['schema_version']}",
        f"- Mode: {'dry-run' if dry_run else 'confirm-run'}",
        f"- Codex CLI invoked: {codex_invoked}",
        f"- Review status: {review_status}",
        f"- Risk level: {payload['risk_level']}",
        f"- Validation authority: {payload['validation_authority']}",
        f"- Human review required: {payload['human_review_required']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Generated at: {payload['generated_at']}",
        "",
        "## Status",
        "",
    ]
    if dry_run:
        lines.append(
            "This is a dry-run advisory review. Codex CLI was not invoked and no "
            "subprocess was run. No findings were produced."
        )
    else:
        lines.append(
            "This is a confirm-run advisory review. Codex CLI was invoked as an "
            "advisory reviewer only. Its output is advisory signal, never a gate "
            "decision."
        )

    summary = payload.get("summary") or ""
    lines.extend(["", "## Summary", "", summary if summary else "(no summary)"])

    for header, key in (
        ("Design Findings", "design_findings"),
        ("Correctness Findings", "correctness_findings"),
        ("Test Coverage Findings", "test_coverage_findings"),
        ("Architecture Boundary Findings", "architecture_boundary_findings"),
        ("Recommended Human Focus", "recommended_human_focus"),
        ("Suggested Followups", "suggested_followups"),
    ):
        items = payload.get(key) or []
        lines.extend(["", f"## {header}", ""])
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- (none)")

    checklist = payload.get("review_checklist")
    lines.extend(["", "## Review Checklist", ""])
    if isinstance(checklist, dict):
        for area in REVIEW_CHECKLIST_AREAS:
            entry = checklist.get(area)
            if isinstance(entry, dict):
                status = entry.get("status")
                summary_text = entry.get("summary") or "(no summary)"
                findings = entry.get("findings") or []
            else:
                status = "(missing)"
                summary_text = "(missing)"
                findings = []
            lines.append(f"- {area}: {status} — {summary_text}")
            for finding in findings:
                lines.append(f"  - {finding}")
    else:
        lines.append("- (no checklist present)")

    priorities = payload.get("human_review_priorities") or []
    lines.extend(["", "## Human Review Priorities", ""])
    if priorities:
        for item in priorities:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('priority')}] {item.get('area')}: "
                f"{item.get('reason')}"
            )
            for check in item.get("suggested_checks") or []:
                lines.append(f"  - {check}")
    else:
        lines.append("- (none)")

    tool_error = payload.get("tool_error")
    if tool_error:
        lines.extend(
            [
                "",
                "## Tool Error",
                "",
                f"- Category: {tool_error.get('category')}",
                f"- Message: {tool_error.get('message')}",
            ]
        )

    invocation = payload.get("codex_invocation")
    if invocation:
        lines.extend(
            [
                "",
                "## Codex Invocation",
                "",
                f"- Command: {invocation.get('command')}",
                f"- Cwd: {invocation.get('cwd')}",
                f"- Timeout seconds: {invocation.get('timeout_seconds')}",
                f"- Duration seconds: {invocation.get('duration_seconds')}",
                f"- Timed out: {invocation.get('timed_out')}",
                f"- Exit code: {invocation.get('exit_code')}",
                f"- Stdout artifact: {invocation.get('stdout_path')}",
                f"- Stderr artifact: {invocation.get('stderr_path')}",
            ]
        )

    if review_status in ("high_risk", "needs_attention", "tool_error"):
        lines.extend(
            [
                "",
                "## Advisory Note",
                "",
                "This is advisory signal only. Human review remains required. "
                "This does not block or approve the task.",
            ]
        )

    lines.extend(["", "## Evidence Detected", ""])
    if present:
        lines.extend(f"- {name}: present" for name in present)
    else:
        lines.append("- (no known evidence files present)")
    if logs:
        lines.append("")
        lines.append("Executor logs:")
        lines.extend(f"- {item['name']}" for item in logs)

    lines.extend(["", "## Missing Evidence", ""])
    if missing:
        lines.extend(f"- {name}" for name in missing)
    else:
        lines.append("- (none)")

    lines.extend(
        [
            "",
            "## Governance",
            "",
            "- Codex is an advisory reviewer only.",
            "- Codex is not deterministic validation authority.",
            "- Codex must not approve, block, merge, push, cleanup, delete "
            "branches, delete worktrees, or change lifecycle.",
            "- Human final approval is required.",
            "- Deterministic validators remain pytest / compileall / policy / "
            "changed-files.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class CodexInvocationOutcome:
    """Normalized result of a single Codex CLI invocation attempt."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    error_kind: str | None  # None | "command_not_found" | "os_error"
    error_message: str | None


def parse_codex_command(codex_command: str) -> list[str]:
    """Split ``codex_command`` safely into argv. Never uses a shell."""

    args = shlex.split(codex_command or "")
    if not args:
        raise CodexAdvisoryReviewError("codex_command must not be empty")
    return args


def invoke_codex_cli(
    command_args: list[str],
    prompt_text: str,
    *,
    cwd: str | None,
    timeout_seconds: int,
) -> CodexInvocationOutcome:
    """Invoke the Codex CLI once with ``shell=False`` and capture its output.

    The advisory prompt is sent on stdin. Never raises for tool failures;
    timeouts, missing commands, and OS errors are normalized into the returned
    outcome so the caller can render a ``tool_error`` advisory artifact.
    """

    start = time.monotonic()
    try:
        completed = subprocess.run(
            command_args,
            input=prompt_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=cwd,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CodexInvocationOutcome(
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            exit_code=None,
            timed_out=True,
            duration_seconds=time.monotonic() - start,
            error_kind=None,
            error_message=f"Codex CLI timed out after {timeout_seconds}s",
        )
    except FileNotFoundError as exc:
        return CodexInvocationOutcome(
            stdout="",
            stderr="",
            exit_code=None,
            timed_out=False,
            duration_seconds=time.monotonic() - start,
            error_kind="command_not_found",
            error_message=f"Codex command not found: {exc}",
        )
    except OSError as exc:
        return CodexInvocationOutcome(
            stdout="",
            stderr="",
            exit_code=None,
            timed_out=False,
            duration_seconds=time.monotonic() - start,
            error_kind="os_error",
            error_message=f"Codex CLI could not be executed: {exc}",
        )
    return CodexInvocationOutcome(
        stdout=_as_text(completed.stdout),
        stderr=_as_text(completed.stderr),
        exit_code=completed.returncode,
        timed_out=False,
        duration_seconds=time.monotonic() - start,
        error_kind=None,
        error_message=None,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_codex_output(stdout: str) -> dict[str, Any]:
    """Parse Codex stdout into a JSON object.

    Supports a raw JSON object or a JSON object inside a fenced code block.
    Raises ``CodexAdvisoryReviewError`` if no JSON object can be parsed.
    """

    text = (stdout or "").strip()
    if not text:
        raise CodexAdvisoryReviewError("Codex stdout was empty; nothing to parse")

    candidates = [text, *_FENCED_BLOCK_RE.findall(text)]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    raise CodexAdvisoryReviewError(
        "Codex stdout could not be parsed into a JSON object"
    )


def merge_codex_findings(payload: dict[str, Any], codex_data: dict[str, Any]) -> None:
    """Merge Codex-provided advisory fields into ``payload`` in place.

    Validates every contributed field *before* mutating ``payload`` so that an
    invariant violation leaves the canonical payload untouched. Raises
    ``CodexAdvisoryReviewError`` on any invariant violation, invalid
    review_status/risk_level, or wrong field type.
    """

    if not isinstance(codex_data, dict):
        raise CodexAdvisoryReviewError("Codex output must be a JSON object")

    if "validation_authority" in codex_data and bool(
        codex_data["validation_authority"]
    ):
        raise CodexAdvisoryReviewError(
            "Codex attempted to set validation_authority=true; "
            "validation_authority is always false"
        )
    if "human_review_required" in codex_data and not bool(
        codex_data["human_review_required"]
    ):
        raise CodexAdvisoryReviewError(
            "Codex attempted to set human_review_required=false; "
            "human_review_required is always true"
        )

    if "review_status" in codex_data:
        review_status = codex_data["review_status"]
        if review_status not in ALLOWED_REVIEW_STATUSES:
            raise CodexAdvisoryReviewError(
                f"Codex returned invalid review_status {review_status!r}"
            )
    if "risk_level" in codex_data:
        risk_level = codex_data["risk_level"]
        if risk_level not in ALLOWED_RISK_LEVELS:
            raise CodexAdvisoryReviewError(
                f"Codex returned invalid risk_level {risk_level!r}"
            )
    if "summary" in codex_data and not isinstance(codex_data["summary"], str):
        raise CodexAdvisoryReviewError("Codex summary must be a string")
    for field in _CODEX_LIST_FIELDS:
        if field in codex_data and not isinstance(codex_data[field], list):
            raise CodexAdvisoryReviewError(f"Codex field {field!r} must be a list")

    # Validate the v0.2.6 checklist contributions before mutating anything so a
    # malformed checklist leaves the canonical (valid) default checklist intact.
    merged_checklist: dict[str, Any] | None = None
    if "review_checklist" in codex_data:
        merged_checklist = _validated_codex_checklist(
            payload.get("review_checklist"), codex_data["review_checklist"]
        )
    validated_priorities: list[dict[str, Any]] | None = None
    if "human_review_priorities" in codex_data:
        validated_priorities = _validated_codex_priorities(
            codex_data["human_review_priorities"]
        )

    for field in CODEX_MUTABLE_FIELDS:
        if field in codex_data:
            payload[field] = codex_data[field]
    if merged_checklist is not None:
        payload["review_checklist"] = merged_checklist
    if validated_priorities is not None:
        payload["human_review_priorities"] = validated_priorities


def _validated_codex_checklist(
    default_checklist: Any,
    codex_checklist: Any,
) -> dict[str, Any]:
    """Validate a Codex-provided checklist and merge it over the default.

    Raises ``CodexAdvisoryReviewError`` if the checklist or any contributed area
    is structurally invalid. Areas Codex omits keep the canonical default entry,
    guaranteeing every required area remains covered.
    """

    if not isinstance(codex_checklist, dict):
        raise CodexAdvisoryReviewError("Codex review_checklist must be an object")

    merged: dict[str, Any] = dict(default_checklist or {})
    for area, entry in codex_checklist.items():
        if not isinstance(entry, dict):
            raise CodexAdvisoryReviewError(
                f"Codex review_checklist area {area!r} must be an object"
            )
        status = entry.get("status")
        if status not in ALLOWED_CHECKLIST_STATUSES:
            raise CodexAdvisoryReviewError(
                f"Codex review_checklist area {area!r} has invalid status "
                f"{status!r}"
            )
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise CodexAdvisoryReviewError(
                f"Codex review_checklist area {area!r} must have a non-empty "
                "summary"
            )
        findings = entry.get("findings", [])
        if not isinstance(findings, list):
            raise CodexAdvisoryReviewError(
                f"Codex review_checklist area {area!r} findings must be a list"
            )
        merged[area] = {
            "status": status,
            "summary": summary,
            "findings": list(findings),
        }
    return merged


def _validated_codex_priorities(codex_priorities: Any) -> list[dict[str, Any]]:
    """Validate a Codex-provided human reviewer priority list.

    Raises ``CodexAdvisoryReviewError`` if the list or any entry is malformed.
    """

    if not isinstance(codex_priorities, list):
        raise CodexAdvisoryReviewError(
            "Codex human_review_priorities must be a list"
        )
    if not codex_priorities:
        raise CodexAdvisoryReviewError(
            "Codex human_review_priorities must be non-empty"
        )
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(codex_priorities):
        if not isinstance(item, dict):
            raise CodexAdvisoryReviewError(
                f"Codex human_review_priorities entry {index} must be an object"
            )
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority <= 0:
            raise CodexAdvisoryReviewError(
                f"Codex human_review_priorities entry {index} must have a "
                "positive integer priority"
            )
        area = item.get("area")
        if area not in ALLOWED_PRIORITY_AREAS:
            raise CodexAdvisoryReviewError(
                f"Codex human_review_priorities entry {index} has invalid area "
                f"{area!r}"
            )
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CodexAdvisoryReviewError(
                f"Codex human_review_priorities entry {index} must have a "
                "non-empty reason"
            )
        suggested_checks = item.get("suggested_checks", [])
        if not isinstance(suggested_checks, list):
            raise CodexAdvisoryReviewError(
                f"Codex human_review_priorities entry {index} suggested_checks "
                "must be a list"
            )
        validated.append(
            {
                "priority": priority,
                "area": area,
                "reason": reason,
                "suggested_checks": list(suggested_checks),
            }
        )
    return validated


def _apply_tool_error(
    payload: dict[str, Any],
    invocation_meta: dict[str, Any],
    *,
    category: str,
    message: str,
    parse_error: str | None = None,
) -> None:
    """Force a payload into a valid ``tool_error`` advisory state."""

    payload["review_status"] = TOOL_ERROR_REVIEW_STATUS
    payload["risk_level"] = TOOL_ERROR_RISK_LEVEL
    payload["validation_authority"] = False
    payload["human_review_required"] = True
    # Codex never produced a real review, so fall back to a structurally valid
    # checklist of `unknown` findings rather than leaving a partial/stale one.
    payload["review_checklist"] = build_default_checklist(
        status=TOOL_ERROR_CHECKLIST_STATUS,
        summary=TOOL_ERROR_CHECKLIST_SUMMARY,
    )
    payload["human_review_priorities"] = build_default_human_review_priorities(
        reason=TOOL_ERROR_PRIORITY_REASON,
    )
    tool_error = {"category": category, "message": message}
    payload["tool_error"] = tool_error
    invocation_meta["tool_error"] = tool_error
    invocation_meta["parse_error"] = parse_error


def build_confirm_run_payload(
    request: CodexAdvisoryReviewRequest,
    evidence: dict[str, Any],
    prompt_text: str,
    *,
    stdout_path: Path,
    stderr_path: Path,
    generated_at: str | None = None,
) -> tuple[dict[str, Any], CodexInvocationOutcome]:
    """Invoke Codex, capture output, and build a confirm-run advisory payload.

    The caller is responsible for writing the stdout/stderr artifacts using the
    returned outcome. Hard invariants are always enforced and any tool/parse/
    invariant failure is downgraded to a ``tool_error`` advisory payload.
    """

    command_args = parse_codex_command(request.codex_command)
    cwd = request.worktree_path or request.repo_path
    cwd_str = str(cwd) if cwd is not None else None

    outcome = invoke_codex_cli(
        command_args,
        prompt_text,
        cwd=cwd_str,
        timeout_seconds=request.timeout_seconds,
    )

    payload = build_review_payload(request, evidence, generated_at=generated_at)
    payload["codex_cli_invoked"] = True
    payload["subprocess_invoked"] = True
    payload["governance"]["codex_cli_invoked"] = True
    payload["governance"]["subprocess_invoked"] = True
    payload["artifacts"]["codex_outputs"] = {
        STDOUT_FILENAME: str(stdout_path),
        STDERR_FILENAME: str(stderr_path),
    }

    invocation_meta: dict[str, Any] = {
        "command": list(command_args),
        "cwd": cwd_str,
        "timeout_seconds": request.timeout_seconds,
        "duration_seconds": round(outcome.duration_seconds, 6),
        "timed_out": outcome.timed_out,
        "exit_code": outcome.exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "parse_error": None,
        "tool_error": None,
    }

    if outcome.timed_out:
        _apply_tool_error(
            payload,
            invocation_meta,
            category="codex_cli_timeout",
            message=outcome.error_message or "Codex CLI timed out",
        )
    elif outcome.error_kind == "command_not_found":
        _apply_tool_error(
            payload,
            invocation_meta,
            category="codex_cli_not_found",
            message=outcome.error_message or "Codex command not found",
        )
    elif outcome.error_kind is not None:
        _apply_tool_error(
            payload,
            invocation_meta,
            category="codex_cli_error",
            message=outcome.error_message or "Codex CLI could not be executed",
        )
    elif outcome.exit_code != 0:
        _apply_tool_error(
            payload,
            invocation_meta,
            category="codex_cli_nonzero_exit",
            message=f"Codex CLI exited with code {outcome.exit_code}",
        )
    else:
        try:
            codex_data = parse_codex_output(outcome.stdout)
        except CodexAdvisoryReviewError as exc:
            _apply_tool_error(
                payload,
                invocation_meta,
                category="codex_output_parse_error",
                message=str(exc),
                parse_error=str(exc),
            )
        else:
            try:
                merge_codex_findings(payload, codex_data)
            except CodexAdvisoryReviewError as exc:
                _apply_tool_error(
                    payload,
                    invocation_meta,
                    category="codex_output_invariant_violation",
                    message=str(exc),
                )

    payload["codex_invocation"] = invocation_meta
    return payload, outcome


def generate_codex_advisory_review(
    request: CodexAdvisoryReviewRequest,
) -> CodexAdvisoryReviewResult:
    """Generate Codex advisory review artifacts in ``artifact_dir``.

    Always writes ``codex-advisory-review-prompt.md``,
    ``codex-advisory-review.json``, and ``codex-advisory-review.md``. In dry-run
    mode (the default) no subprocess is invoked. In confirm-run mode the Codex
    CLI is invoked exactly once and ``codex-advisory-review-stdout.txt`` /
    ``codex-advisory-review-stderr.txt`` are also written.

    This function never approves, blocks, merges, pushes, cleans up, deletes
    branches/worktrees, or changes task lifecycle.
    """

    artifact_dir = request.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)

    evidence = detect_evidence(artifact_dir)
    prompt_text = build_review_prompt(request, evidence)

    prompt_path = artifact_dir / PROMPT_FILENAME
    json_path = artifact_dir / JSON_FILENAME
    markdown_path = artifact_dir / MARKDOWN_FILENAME
    atomic_write_text(prompt_path, prompt_text)

    stdout_path: Path | None = None
    stderr_path: Path | None = None

    if request.confirm_run:
        stdout_path = artifact_dir / STDOUT_FILENAME
        stderr_path = artifact_dir / STDERR_FILENAME
        payload, outcome = build_confirm_run_payload(
            request,
            evidence,
            prompt_text,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        atomic_write_text(stdout_path, outcome.stdout)
        atomic_write_text(stderr_path, outcome.stderr)
    else:
        payload = build_review_payload(request, evidence)

    validate_payload(payload)

    markdown_text = build_review_markdown(payload)
    atomic_write_json(json_path, payload, sort_keys=True)
    atomic_write_text(markdown_path, markdown_text)

    return CodexAdvisoryReviewResult(
        task_key=request.task_key,
        artifact_dir=artifact_dir,
        prompt_path=prompt_path,
        json_path=json_path,
        markdown_path=markdown_path,
        payload=payload,
        evidence=evidence,
        dry_run=bool(request.dry_run),
        confirm_run=bool(request.confirm_run),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


__all__ = [
    "ALLOWED_CHECKLIST_STATUSES",
    "ALLOWED_PRIORITY_AREAS",
    "ALLOWED_REVIEW_STATUSES",
    "ALLOWED_RISK_LEVELS",
    "CODEX_MUTABLE_FIELDS",
    "REVIEW_CHECKLIST_AREAS",
    "CodexAdvisoryReviewError",
    "CodexAdvisoryReviewRequest",
    "CodexAdvisoryReviewResult",
    "CodexInvocationOutcome",
    "DEFAULT_CODEX_COMMAND",
    "DEFAULT_TIMEOUT_SECONDS",
    "DRY_RUN_REVIEW_STATUS",
    "DRY_RUN_RISK_LEVEL",
    "GENERATED_ARTIFACT_FILENAMES",
    "JSON_FILENAME",
    "KNOWN_EVIDENCE_FILES",
    "MARKDOWN_FILENAME",
    "PROMPT_FILENAME",
    "REVIEW_DIMENSIONS",
    "REVIEWER",
    "SCHEMA_VERSION",
    "SOURCE",
    "STDERR_FILENAME",
    "STDOUT_FILENAME",
    "TOOL_ERROR_REVIEW_STATUS",
    "TOOL_ERROR_RISK_LEVEL",
    "build_confirm_run_payload",
    "build_default_checklist",
    "build_default_human_review_priorities",
    "build_review_markdown",
    "build_review_payload",
    "build_review_prompt",
    "detect_evidence",
    "generate_codex_advisory_review",
    "invoke_codex_cli",
    "merge_codex_findings",
    "parse_codex_command",
    "parse_codex_output",
    "validate_payload",
]
