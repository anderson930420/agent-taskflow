"""Codex advisory reviewer dry-run contract.

This module builds a read-only Codex advisory review contract over an existing
task artifact directory. It inspects evidence file presence, renders a Codex CLI
review prompt, and generates dry-run review artifacts for a future Codex CLI
design/code review stage.

This first version (v0.2.1) is dry-run only. It does not invoke the Codex CLI or
any subprocess. The Codex advisory reviewer is advisory only: it is never
deterministic validation authority, and it never approves, blocks, merges,
pushes, cleans up, deletes branches/worktrees, or changes task lifecycle.

Deterministic validators remain pytest / compileall / policy / changed-files.
Human final approval is always required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.models import utc_now_iso
from agent_taskflow.tasks import normalize_task_key


SCHEMA_VERSION = "codex_advisory_review.v1"
REVIEWER = "codex-cli"
SOURCE = "codex_advisory_review"

PROMPT_FILENAME = "codex-advisory-review-prompt.md"
JSON_FILENAME = "codex-advisory-review.json"
MARKDOWN_FILENAME = "codex-advisory-review.md"
GENERATED_ARTIFACT_FILENAMES = (
    PROMPT_FILENAME,
    JSON_FILENAME,
    MARKDOWN_FILENAME,
)

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

DRY_RUN_REVIEW_STATUS = "not_run"
DRY_RUN_RISK_LEVEL = "unknown"

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
    """Request for generating a dry-run Codex advisory review."""

    task_key: str
    artifact_dir: Path
    repo_path: Path | None = None
    worktree_path: Path | None = None
    dry_run: bool = True

    def __post_init__(self) -> None:
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

    def artifact_paths(self) -> list[Path]:
        return [self.prompt_path, self.json_path, self.markdown_path]


def _normalize_path(path: str | Path, *, name: str) -> Path:
    if path is None:
        raise ValueError(f"{name} must not be None")
    return Path(path).expanduser().resolve()


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
        "artifacts": artifacts,
        "dry_run": bool(request.dry_run),
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
    """Render a human-readable dry-run review summary."""

    evidence = payload.get("evidence", {})
    present = evidence.get("present_evidence", [])
    missing = payload.get("missing_evidence", [])
    logs = evidence.get("executor_logs", [])

    lines = [
        "# Codex Advisory Review (Dry Run)",
        "",
        f"- Task key: {payload['task_key']}",
        f"- Reviewer: {payload['reviewer']}",
        f"- Schema version: {payload['schema_version']}",
        f"- Review status: {payload['review_status']}",
        f"- Risk level: {payload['risk_level']}",
        f"- Validation authority: {payload['validation_authority']}",
        f"- Human review required: {payload['human_review_required']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Generated at: {payload['generated_at']}",
        "",
        "## Status",
        "",
        "This is a dry-run advisory review. Codex CLI was not invoked and no "
        "subprocess was run. No findings were produced.",
        "",
        "## Review Dimensions",
        "",
    ]
    lines.extend(f"- {dimension}" for dimension in payload["review_dimensions"])
    lines.extend(
        [
            "",
            "## Evidence Detected",
            "",
        ]
    )
    if present:
        lines.extend(f"- {name}: present" for name in present)
    else:
        lines.append("- (no known evidence files present)")
    if logs:
        lines.append("")
        lines.append("Executor logs:")
        lines.extend(f"- {item['name']}" for item in logs)
    lines.extend(
        [
            "",
            "## Missing Evidence",
            "",
        ]
    )
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


def generate_codex_advisory_review(
    request: CodexAdvisoryReviewRequest,
) -> CodexAdvisoryReviewResult:
    """Generate dry-run Codex advisory review artifacts in ``artifact_dir``.

    Writes ``codex-advisory-review-prompt.md``, ``codex-advisory-review.json``,
    and ``codex-advisory-review.md`` into the artifact directory. Does not invoke
    subprocess, Codex CLI, executors, validators, or lifecycle mutation.
    """

    artifact_dir = request.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)

    evidence = detect_evidence(artifact_dir)
    payload = build_review_payload(request, evidence)
    validate_payload(payload)

    prompt_text = build_review_prompt(request, evidence)
    markdown_text = build_review_markdown(payload)

    prompt_path = artifact_dir / PROMPT_FILENAME
    json_path = artifact_dir / JSON_FILENAME
    markdown_path = artifact_dir / MARKDOWN_FILENAME

    prompt_path.write_text(prompt_text, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(markdown_text, encoding="utf-8")

    return CodexAdvisoryReviewResult(
        task_key=request.task_key,
        artifact_dir=artifact_dir,
        prompt_path=prompt_path,
        json_path=json_path,
        markdown_path=markdown_path,
        payload=payload,
        evidence=evidence,
        dry_run=bool(request.dry_run),
    )


__all__ = [
    "ALLOWED_REVIEW_STATUSES",
    "ALLOWED_RISK_LEVELS",
    "CodexAdvisoryReviewError",
    "CodexAdvisoryReviewRequest",
    "CodexAdvisoryReviewResult",
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
    "build_review_markdown",
    "build_review_payload",
    "build_review_prompt",
    "detect_evidence",
    "generate_codex_advisory_review",
    "validate_payload",
]
