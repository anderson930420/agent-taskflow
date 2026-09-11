"""Read-only V1 Step 4 concurrency-readiness gate (SPEC §19.4).

``max_concurrent_tasks`` may rise above 1 only when the Step 4 rehearsal
evidence shows that every §19.1-§19.3 check passed. This module decides that,
the way ``m1_exit_gate`` audits M1 evidence: it reads one JSON file, never
writes, and fails closed on anything it cannot verify.

The evidence must also be bound to the repository it is used from: its
``repo_sha`` has to equal that repository's ``HEAD``, so evidence produced by
other code cannot unlock concurrency.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

CONCURRENCY_GATE_SCHEMA_VERSION = "v1_step4_concurrency_gate.v1"
CONCURRENCY_REHEARSAL_SCHEMA_VERSION = "v1_step4_concurrency_rehearsal.v1"
CONCURRENCY_EVIDENCE_FILENAME = "concurrency-rehearsal.json"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_CONCURRENCY_CHECKS: dict[str, tuple[str, ...]] = {
    "19.1": (
        "atomic_claim_threads_explicit_single_winner",
        "atomic_claim_processes_explicit_single_winner",
        "atomic_claim_threads_dispatcher_single_winner",
        "atomic_claim_processes_dispatcher_single_winner",
        "atomic_claim_losers_typed_refusal",
        "atomic_claim_single_attempt_and_lease",
    ),
    "19.2": (
        "concurrent_writes_no_lost_write",
        "concurrent_writes_integrity_check_ok",
        "concurrent_writes_lifecycle_log_valid",
        "concurrent_writes_contention_observable",
    ),
    "19.3": (
        "crash_holder_sigkilled",
        "crash_lease_expired",
        "crash_ticket_recovered_via_retry",
        "crash_nothing_running_forever",
        "crash_no_double_ownership",
        "crash_previous_attempt_auditable",
    ),
}


def git_head(repo_root: str | Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(repo_root),
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _load(path: Path) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, None, f"cannot read evidence {path}: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, raw, f"invalid evidence JSON {path}: {exc}"
    if not isinstance(payload, dict):
        return None, raw, f"invalid evidence {path}: top-level JSON must be an object"
    return payload, raw, None


def evaluate_concurrency_evidence(
    evidence_path: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic, read-only verdict on Step 4 rehearsal evidence."""
    path = Path(evidence_path).expanduser()
    root = Path(repo_root) if repo_root is not None else DEFAULT_REPO_ROOT
    payload, raw, load_error = _load(path)
    errors: list[str] = [] if load_error is None else [load_error]
    audited_sha = git_head(root)

    if payload is not None:
        if payload.get("schema_version") != CONCURRENCY_REHEARSAL_SCHEMA_VERSION:
            errors.append(f"schema_version must be {CONCURRENCY_REHEARSAL_SCHEMA_VERSION}")
        if audited_sha is None:
            errors.append(f"cannot resolve git HEAD of {root}")
        elif payload.get("repo_sha") != audited_sha:
            errors.append("repo_sha must match the audited repository HEAD")
        if payload.get("disposable_database") is not True:
            errors.append("disposable_database must be true")
        if payload.get("production_database_touched") is not False:
            errors.append("production_database_touched must be false")
        checks = payload.get("checks")
        if not isinstance(checks, dict):
            errors.append("checks must be an object")
            checks = {}
        for section, names in REQUIRED_CONCURRENCY_CHECKS.items():
            for name in names:
                if checks.get(name) is not True:
                    errors.append(f"§{section} check {name} must be true")
        if payload.get("all_checks_passed") is not True:
            errors.append("all_checks_passed must be true")

    return {
        "schema_version": CONCURRENCY_GATE_SCHEMA_VERSION,
        "gate": "blocked" if errors else "passed",
        "errors": errors,
        "evidence_path": str(path),
        "evidence_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "evidence_schema_version": (
            payload.get("schema_version") if payload is not None else None
        ),
        "evidence_repo_sha": payload.get("repo_sha") if payload is not None else None,
        "audited_repo_root": str(root),
        "audited_repo_sha": audited_sha,
        "required_checks": {
            section: list(names) for section, names in REQUIRED_CONCURRENCY_CHECKS.items()
        },
        "read_only": True,
    }


__all__ = [
    "CONCURRENCY_EVIDENCE_FILENAME",
    "CONCURRENCY_GATE_SCHEMA_VERSION",
    "CONCURRENCY_REHEARSAL_SCHEMA_VERSION",
    "REQUIRED_CONCURRENCY_CHECKS",
    "evaluate_concurrency_evidence",
    "git_head",
]
