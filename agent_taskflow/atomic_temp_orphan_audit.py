"""Read-only inventory of orphan temporary files from atomic writes.

``agent_taskflow.atomic_write`` creates same-directory temporary files named
``.{target.name}.{16 lowercase hex characters}.tmp``. Normal exception paths
attempt to remove those files, but a process crash or SIGKILL can leave one
behind. This module makes those candidates visible without cleaning them up or
mutating any other project state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from typing import Any


ATOMIC_TEMP_ORPHAN_AUDIT_SCHEMA_VERSION = "atomic_temp_orphan_audit.v1"
ATOMIC_TEMP_ORPHAN_AUDIT_SOURCE = "atomic_temp_orphan_audit"
DEFAULT_MAX_ENTRIES = 100

ATOMIC_TEMP_FILE_PATTERN = re.compile(
    r"^\.(?P<target_name>.+)\.(?P<random_segment>[0-9a-f]{16})\.tmp$"
)


@dataclass(frozen=True)
class AtomicTempOrphanAuditRequest:
    """Inputs for one read-only orphan temporary-file audit."""

    roots: tuple[Path, ...] = field(default_factory=lambda: (Path("."),))
    max_entries: int = DEFAULT_MAX_ENTRIES

    def __post_init__(self) -> None:
        normalized_roots = tuple(
            Path(root).expanduser().absolute() for root in self.roots
        )
        if not normalized_roots:
            raise ValueError("at least one root is required")
        if self.max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        object.__setattr__(self, "roots", normalized_roots)


def summarize_atomic_temp_orphans(
    request: AtomicTempOrphanAuditRequest,
) -> dict[str, Any]:
    """Return a read-only audit of matching atomic-write temporary files.

    Missing or inaccessible roots and entries are represented as warnings. A
    warning does not make the audit unsuccessful because all reachable roots
    were still inspected and no mutation is attempted.
    """

    warnings: list[str] = []
    orphan_temp_files: list[dict[str, Any]] = []
    orphan_temp_count = 0

    for root in request.roots:
        for candidate in _matching_candidates(root, warnings):
            item = _inspect_candidate(candidate, root=root, warnings=warnings)
            if item is None:
                continue
            orphan_temp_count += 1
            if len(orphan_temp_files) < request.max_entries:
                orphan_temp_files.append(item)

    truncated = orphan_temp_count > len(orphan_temp_files)
    return {
        "ok": True,
        "schema_version": ATOMIC_TEMP_ORPHAN_AUDIT_SCHEMA_VERSION,
        "source": ATOMIC_TEMP_ORPHAN_AUDIT_SOURCE,
        "roots": [str(root) for root in request.roots],
        "orphan_temp_files": orphan_temp_files,
        "summary": {
            "root_count": len(request.roots),
            "orphan_temp_count": orphan_temp_count,
            "warning_count": len(warnings),
            "truncated": truncated,
        },
        "warnings": warnings,
        "safety": atomic_temp_orphan_audit_safety_flags(),
    }


def atomic_temp_orphan_audit_safety_flags() -> dict[str, bool]:
    """Return explicit safety assertions for this read-only audit."""

    return {
        "read_only": True,
        "files_deleted": False,
        "files_modified": False,
        "db_written": False,
        "gitignore_modified": False,
        "changed_files_validator_modified": False,
        "changed_files_exclusion_added": False,
        "cleanup_performed": False,
        "executor_started": False,
        "validator_started": False,
        "approved": False,
        "merged": False,
    }


def _matching_candidates(root: Path, warnings: list[str]) -> list[Path]:
    """Return matching entries below ``root`` without following symlinks."""

    try:
        root_stat = os.stat(root)
    except OSError as exc:
        warnings.append(f"could not inspect root {root}: {exc}")
        return []
    if not stat.S_ISDIR(root_stat.st_mode):
        warnings.append(f"root is not a directory: {root}")
        return []

    matches: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            warnings.append(f"could not scan directory {directory}: {exc}")
            continue

        child_directories: list[Path] = []
        for entry in entries:
            entry_path = Path(entry.path)
            if ATOMIC_TEMP_FILE_PATTERN.fullmatch(entry.name):
                matches.append(entry_path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(entry_path)
            except OSError as exc:
                warnings.append(f"could not inspect entry {entry_path}: {exc}")

        # Reverse push order so the lexical first child is scanned first.
        pending.extend(reversed(child_directories))

    return matches


def _inspect_candidate(
    candidate: Path,
    *,
    root: Path,
    warnings: list[str],
) -> dict[str, Any] | None:
    match = ATOMIC_TEMP_FILE_PATTERN.fullmatch(candidate.name)
    if match is None:
        return None
    try:
        candidate_stat = os.lstat(candidate)
    except OSError as exc:
        warnings.append(f"could not inspect candidate {candidate}: {exc}")
        return None

    target_name = match.group("target_name")
    return {
        "path": str(candidate),
        "root": str(root),
        "candidate_target_path": str(candidate.parent / target_name),
        "candidate_target_name": target_name,
        "random_segment": match.group("random_segment"),
        "size_bytes": candidate_stat.st_size,
        "mtime_ns": candidate_stat.st_mtime_ns,
        "is_regular_file": stat.S_ISREG(candidate_stat.st_mode),
    }


def render_atomic_temp_orphan_audit_summary(audit: dict[str, Any]) -> str:
    """Render a human-readable view of an orphan temporary-file audit."""

    stats = audit.get("summary") or {}
    orphan_temp_files = audit.get("orphan_temp_files") or []
    lines = [
        "Atomic Temp Orphan Audit (read-only)",
        "====================================",
        "",
        "Roots:",
    ]
    for root in audit.get("roots") or []:
        lines.append(f"  - {root}")

    lines.extend(
        [
            "",
            f"Orphan temp files ({stats.get('orphan_temp_count', 0)}):",
        ]
    )
    for item in orphan_temp_files:
        lines.append(f"  - {item.get('path')}")
        lines.append(f"      candidate target: {item.get('candidate_target_path')}")
        lines.append(
            "      size_bytes="
            f"{item.get('size_bytes')} regular_file={item.get('is_regular_file')}"
        )
    if stats.get("truncated"):
        lines.append("  (entry list truncated)")

    lines.extend(
        [
            "",
            "Summary:",
            f"  roots: {stats.get('root_count', 0)}",
            f"  orphan temp files: {stats.get('orphan_temp_count', 0)}",
            f"  warnings: {stats.get('warning_count', 0)}",
            f"  truncated: {stats.get('truncated', False)}",
        ]
    )

    warnings = audit.get("warnings") or []
    if warnings:
        lines.extend(["", "Warnings:"])
        for warning in warnings:
            lines.append(f"  - {warning}")

    lines.extend(
        [
            "",
            "Safety: read-only audit; no files were deleted or modified, no DB "
            "records were written, and no cleanup, executor, or validator was run.",
            "",
        ]
    )
    return "\n".join(lines)
