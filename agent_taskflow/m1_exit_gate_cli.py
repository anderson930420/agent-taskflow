"""CLI-facing compatibility correction for the M1 exit-gate audit.

The original reconciliation inspected the thin CLI wrapper for legacy fallback
logic.  The fallback actually lives in ``real_scheduled_execution_observability``.
This module preserves every core gate result and corrects only that repository
fact before the report is rendered.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.m1_exit_gate import (
    VALID_GATE_STATUSES,
    audit_m1_exit_gate as _audit_m1_exit_gate,
)


def _tasks_has_legacy_marker(db_path: Path) -> bool:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
    return any(str(row[1]) == "is_legacy" for row in rows)


def _reader_module_retains_fallback(repo_root: Path) -> tuple[bool, Path]:
    reader = repo_root / "agent_taskflow" / "real_scheduled_execution_observability.py"
    if not reader.is_file():
        return False, reader
    try:
        text = reader.read_text(encoding="utf-8").lower()
    except OSError:
        return False, reader
    required_markers = (
        "fall back to the legacy tick",
        "falls back to the legacy tick payload",
        "legacy ticks",
    )
    return all(marker in text for marker in required_markers), reader


def _recalculate_report(report: dict[str, Any]) -> None:
    counts = {status: 0 for status in sorted(VALID_GATE_STATUSES)}
    gates = report.get("gates", [])
    for gate in gates:
        counts[str(gate["status"])] += 1
    overall = "passed" if counts["passed"] == len(gates) else (
        "blocked" if counts["blocked"] else "partial"
    )
    report["gate_status_counts"] = counts
    report["m1_exit_gate"] = overall
    report["m2_entry_allowed"] = overall == "passed"
    report["next_required_actions"] = [
        gate["next_action"]
        for gate in gates
        if isinstance(gate, dict) and gate.get("next_action") is not None
    ]


def audit_m1_exit_gate(
    *,
    db_path: str | Path,
    repo_root: str | Path,
    evidence_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the core audit and correct the legacy-reader repository fact."""
    report = _audit_m1_exit_gate(
        db_path=db_path,
        repo_root=repo_root,
        evidence_dir=evidence_dir,
    )
    db = Path(db_path).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    has_marker = _tasks_has_legacy_marker(db)
    has_fallback, reader = _reader_module_retains_fallback(repo)

    for gate in report["gates"]:
        if gate.get("gate") != "legacy_schema_reader_retained":
            continue
        gate["evidence"] = [
            f"tasks.is_legacy={str(has_marker).lower()}",
            str(reader),
            f"reader_fallback_verified={str(has_fallback).lower()}",
        ]
        if has_marker and has_fallback:
            gate["status"] = "passed"
            gate["summary"] = (
                "Legacy task marker and legacy observability fallback reader are retained."
            )
            gate.pop("next_action", None)
        else:
            missing = []
            if not has_marker:
                missing.append("tasks.is_legacy")
            if not has_fallback:
                missing.append("legacy observability fallback module")
            gate["status"] = "blocked"
            gate["summary"] = f"Missing required legacy retention: {', '.join(missing)}"
            gate["next_action"] = (
                "Restore the legacy schema marker and reader fallback until M1 is formally closed."
            )
        break

    _recalculate_report(report)
    return report


__all__ = ["audit_m1_exit_gate"]
