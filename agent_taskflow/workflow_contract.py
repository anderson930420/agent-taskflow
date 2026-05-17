"""Lightweight WORKFLOW.md contract reader.

This module intentionally performs only static Markdown text inspection. It is
not wired into dispatch, executor selection, validation, API behavior, or any
runtime enforcement path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)

_REQUIRED_SECTION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Purpose", ("purpose",)),
    ("Component Ownership", ("component ownership",)),
    ("Task Lifecycle", ("task lifecycle",)),
    ("Workspace Policy", ("workspace policy",)),
    ("Executor Policy", ("executor policy",)),
    ("Validation Policy", ("validation policy",)),
    ("Changed-Files / Path Policy", ("changed-files / path policy", "changed files path policy")),
    ("Proof-of-Work Artifacts", ("proof-of-work artifacts", "proof of work artifacts")),
    ("Human Review Gate", ("human review gate",)),
    ("Non-Goals", ("non-goals", "non goals")),
    ("Future Machine-Readable Contract", ("future machine-readable contract", "future machine readable contract")),
)


def _normalize_marker(value: str) -> str:
    lowered = value.strip().lower()
    lowered = lowered.replace("`", "")
    lowered = lowered.replace("_", " ")
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered


def _extract_headings(raw_text: str) -> tuple[str, ...]:
    return tuple(_normalize_marker(match.group(1)) for match in _HEADING_RE.finditer(raw_text))


def _contains_any_heading(headings: tuple[str, ...], markers: tuple[str, ...]) -> bool:
    normalized_markers = tuple(_normalize_marker(marker) for marker in markers)
    return any(
        marker in heading
        for heading in headings
        for marker in normalized_markers
    )


def _contains_phrase(raw_text: str, phrase: str) -> bool:
    return _normalize_marker(phrase) in _normalize_marker(raw_text)


@dataclass(frozen=True)
class WorkflowContractValidationResult:
    """Validation result for a parsed WORKFLOW.md contract."""

    source_path: Path
    passed: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WorkflowContract:
    """Minimal structured view of a repo-owned WORKFLOW.md contract."""

    source_path: Path
    raw_text: str
    has_deterministic_orchestration_boundary: bool
    has_component_ownership: bool
    has_task_lifecycle: bool
    has_workspace_policy: bool
    has_executor_policy: bool
    has_validation_policy: bool
    has_changed_files_policy: bool
    has_proof_of_work_artifacts: bool
    has_human_review_gate: bool
    has_non_goals: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> WorkflowContractValidationResult:
        """Validate required WORKFLOW.md sections without side effects."""
        headings = _extract_headings(self.raw_text)
        errors = list(self.errors)

        for label, markers in _REQUIRED_SECTION_MARKERS:
            if not _contains_any_heading(headings, markers):
                errors.append(f"Missing required WORKFLOW.md section: {label}")

        if not self.has_deterministic_orchestration_boundary:
            errors.append(
                "Missing deterministic orchestration boundary language in WORKFLOW.md"
            )

        return WorkflowContractValidationResult(
            source_path=self.source_path,
            passed=not errors,
            errors=tuple(errors),
            warnings=self.warnings,
        )


def load_workflow_contract(path: Path) -> WorkflowContract:
    """Load and inspect a WORKFLOW.md contract.

    The parser reads the file and checks for expected headings and phrases. It
    does not mutate repository state, execute commands, or enforce policy.
    """
    source_path = Path(path)
    try:
        raw_text = source_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"WORKFLOW.md not found: {source_path}") from exc

    headings = _extract_headings(raw_text)
    warnings: list[str] = []

    if not raw_text.strip():
        warnings.append("WORKFLOW.md is empty")

    return WorkflowContract(
        source_path=source_path,
        raw_text=raw_text,
        has_deterministic_orchestration_boundary=(
            _contains_phrase(raw_text, "deterministic Python orchestration code")
            and _contains_phrase(raw_text, "bounded implementation workers")
        ),
        has_component_ownership=_contains_any_heading(headings, ("component ownership",)),
        has_task_lifecycle=_contains_any_heading(headings, ("task lifecycle",)),
        has_workspace_policy=_contains_any_heading(headings, ("workspace policy",)),
        has_executor_policy=_contains_any_heading(headings, ("executor policy",)),
        has_validation_policy=_contains_any_heading(headings, ("validation policy",)),
        has_changed_files_policy=_contains_any_heading(
            headings,
            ("changed-files / path policy", "changed files path policy"),
        ),
        has_proof_of_work_artifacts=_contains_any_heading(
            headings,
            ("proof-of-work artifacts", "proof of work artifacts"),
        ),
        has_human_review_gate=_contains_any_heading(headings, ("human review gate",)),
        has_non_goals=_contains_any_heading(headings, ("non-goals", "non goals")),
        warnings=tuple(warnings),
    )

