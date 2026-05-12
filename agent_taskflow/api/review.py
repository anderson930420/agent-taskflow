"""Read-only review evidence helper for Agent Taskflow.

This module provides helpers for assembling a human-readable review evidence
bundle from the artifact directory. It is intentionally read-only: it never
calls the dispatcher, never modifies state, never approves/rejects, and never
allows arbitrary filesystem access.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_taskflow.mission_contract import read_mission_contract


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

# Maximum number of bytes to inline in a text preview.
_MAX_PREVIEW_SIZE = 20 * 1024  # 20 KB

# Maximum file size before we skip metadata too (1 MB).
_MAX_LIST_SIZE = 1024 * 1024

# Patterns that indicate a high-confidence secret assignment.
# These are the same patterns used by PolicyCheckValidator.
_SECRET_PATTERNS = (
    re.compile(
        r'[A-Z_][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*[:=]',
        re.IGNORECASE,
    ),
    re.compile(
        r'"[A-Za-z_]*(?:api_key|token|secret|password|credential|access_token|refresh_token|authorization)"\s*:\s*"[^"]+',
        re.IGNORECASE,
    ),
    re.compile(
        r'(?:api_key|token|secret)\s*=\s*["\']?(?:sk-|ak-)[A-Za-z0-9_-]{10,}',
        re.IGNORECASE,
    ),
    re.compile(
        r'[A-Z_][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*=\s*\S+',
        re.IGNORECASE,
    ),
)

# Binary file extensions to skip for preview.
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".whl",
    ".pyc", ".pyo", ".so", ".dll", ".exe",
    ".webp", ".svg", ".ico",
})

# Kinds we recognise.
_VALIDATOR_LOG_NAMES = frozenset({
    "pytest.log",
    "openspec-validate.log",
    "policy-validate.log",
    "typecheck.log",
    "lint.log",
})

_CONTRACT_NAME = "mission_contract.json"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _is_binary_suffix(suffix: str) -> bool:
    return suffix.lower() in _BINARY_EXTENSIONS


def _file_kind(name: str) -> str:
    if name == _CONTRACT_NAME:
        return "mission_contract"
    if name in _VALIDATOR_LOG_NAMES:
        return "validator_log"
    if name.startswith("pi-") or name.startswith("opencode-"):
        return "executor_log"
    return "other"


def _scan_for_secrets(text: str) -> bool:
    """Return True if text contains high-confidence secret assignments."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _read_preview(path: Path) -> tuple[str, bool]:
    """Read a preview from path.

    Returns (content, was_truncated).
    Content is empty string if file is binary or too large.
    """
    if _is_binary_suffix(path.suffix):
        return "", False
    try:
        size = path.stat().st_size
    except OSError:
        return "", False
    if size > _MAX_PREVIEW_SIZE:
        # Read only the first MAX_PREVIEW_SIZE bytes.
        try:
            with path.open("rb") as f:
                raw = f.read(_MAX_PREVIEW_SIZE)
            text = raw.decode("utf-8", errors="replace")
            return text, True
        except OSError:
            return "", False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text, False
    except OSError:
        return "", False


def _safe_list_dir(artifact_dir: Path) -> list[Path]:
    """List files in artifact_dir, resolving paths to prevent traversal."""
    try:
        resolved = artifact_dir.resolve()
    except OSError:
        return []
    if not resolved.is_dir():
        return []
    results: list[Path] = []
    for entry in resolved.iterdir():
        try:
            # Ensure the entry is actually inside resolved.
            entry_resolved = entry.resolve()
            if entry_resolved.parent != resolved and entry_resolved != resolved:
                # Skip files outside the artifact dir (symlink traversal).
                continue
        except OSError:
            continue
        results.append(entry_resolved)
    return sorted(results, key=lambda p: p.name)


# ----------------------------------------------------------------------
# Schema builders
# ----------------------------------------------------------------------


def build_contract_summary(artifact_dir: Path) -> dict[str, Any]:
    """Build a mission contract summary dict.

    Returns a dict with keys: exists, status ("present"|"missing"|"invalid"),
    and fields from the contract if present.
    """
    contract_path = artifact_dir / _CONTRACT_NAME
    if not contract_path.exists():
        return {
            "exists": False,
            "status": "missing",
            "schema_version": None,
            "task_key": None,
            "goal": None,
            "executor": None,
            "required_validators": [],
            "forbidden_actions": [],
            "expected_artifacts": [],
            "human_approval_required": None,
            "governance_rules": [],
        }

    try:
        d = read_mission_contract(contract_path)
    except Exception as exc:
        return {
            "exists": False,
            "status": "invalid",
            "error": str(exc),
            "schema_version": None,
            "task_key": None,
            "goal": None,
            "executor": None,
            "required_validators": [],
            "forbidden_actions": [],
            "expected_artifacts": [],
            "human_approval_required": None,
            "governance_rules": [],
        }

    return {
        "exists": True,
        "status": "present",
        "schema_version": d.get("schema_version"),
        "task_key": d.get("task_key"),
        "goal": d.get("goal"),
        "executor": d.get("executor"),
        "required_validators": d.get("required_validators", []),
        "forbidden_actions": d.get("forbidden_actions", []),
        "expected_artifacts": d.get("expected_artifacts", []),
        "human_approval_required": d.get("human_approval_required"),
        "governance_rules": d.get("governance_rules", []),
    }


def build_artifact_file_summaries(artifact_dir: Path) -> list[dict[str, Any]]:
    """Build a list of artifact file summaries from artifact_dir.

    All paths returned are relative to artifact_dir.
    """
    entries = _safe_list_dir(artifact_dir)
    summaries: list[dict[str, Any]] = []

    for path in entries:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        name = path.name
        suffix = path.suffix
        kind = _file_kind(name)
        is_binary = _is_binary_suffix(suffix)
        has_secret = False
        preview_available = False

        if not is_binary and size <= _MAX_PREVIEW_SIZE:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                has_secret = _scan_for_secrets(text)
                # Preview is only available if no secrets detected.
                preview_available = not has_secret
            except OSError:
                pass

        summaries.append({
            "name": name,
            "kind": kind,
            "size_bytes": size,
            "preview_available": preview_available,
            "has_secret_warning": has_secret,
            "is_binary": is_binary,
            "is_validator_log": kind == "validator_log",
            "is_executor_log": kind == "executor_log",
            "is_mission_contract": kind == "mission_contract",
        })

    return summaries


def build_artifact_preview(
    artifact_dir: Path,
    artifact_name: str,
) -> dict[str, Any]:
    """Build a preview for a single artifact file.

    Raises ValueError if artifact_name contains path traversal characters.
    Returns a dict with content (None if not available), truncated flag,
    size_bytes, and error reason if any.
    """
    # Reject traversal: disallow ".." and absolute-looking names.
    if ".." in artifact_name or artifact_name.startswith("/"):
        raise ValueError("artifact_name must not contain '..' or start with '/'")

    resolved_artifact_dir = artifact_dir.resolve()
    target = resolved_artifact_dir / artifact_name
    try:
        target_resolved = target.resolve()
    except OSError as exc:
        raise ValueError(f"Cannot resolve artifact path: {exc}") from exc

    # Ensure target is inside artifact_dir.
    try:
        target_resolved.relative_to(resolved_artifact_dir)
    except ValueError as exc:
        raise ValueError("artifact must be inside the task artifact directory") from exc

    if not target_resolved.is_file():
        raise ValueError(f"artifact is not a file or does not exist: {artifact_name}")

    suffix = target_resolved.suffix
    is_binary = _is_binary_suffix(suffix)

    if is_binary:
        return {
            "name": artifact_name,
            "content": None,
            "truncated": False,
            "size_bytes": 0,
            "preview_reason": "binary file",
        }

    if target_resolved.stat().st_size > _MAX_PREVIEW_SIZE:
        # Read first MAX_PREVIEW_SIZE bytes as binary.
        try:
            with target_resolved.open("rb") as f:
                raw = f.read(_MAX_PREVIEW_SIZE)
            text = raw.decode("utf-8", errors="replace")
            return {
                "name": artifact_name,
                "content": text,
                "truncated": True,
                "size_bytes": target_resolved.stat().st_size,
                "preview_reason": None,
            }
        except OSError as exc:
            return {
                "name": artifact_name,
                "content": None,
                "truncated": False,
                "size_bytes": 0,
                "preview_reason": f"read error: {exc}",
            }

    try:
        text = target_resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "name": artifact_name,
            "content": None,
            "truncated": False,
            "size_bytes": 0,
            "preview_reason": f"read error: {exc}",
        }

    # Check for secrets before returning content.
    if _scan_for_secrets(text):
        return {
            "name": artifact_name,
            "content": None,
            "truncated": False,
            "size_bytes": target_resolved.stat().st_size,
            "preview_reason": "contains high-confidence secret-like assignment; preview not available",
        }

    return {
        "name": artifact_name,
        "content": text,
        "truncated": False,
        "size_bytes": target_resolved.stat().st_size,
        "preview_reason": None,
    }


def build_review_evidence(
    task_key: str,
    artifact_dir: Path,
    validation_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a complete review evidence bundle.

    This is the main entry point for the API endpoint.
    """
    contract = build_contract_summary(artifact_dir)
    files = build_artifact_file_summaries(artifact_dir)

    # Summarise policy status.
    policy_result = next(
        (r for r in validation_results if r.get("validator") == "policy"),
        None,
    )
    if policy_result is None:
        contract_has_policy = "policy" in contract.get("required_validators", [])
        policy_status = "not_required" if not contract_has_policy else "not_run"
    else:
        policy_status = policy_result.get("status", "unknown")

    policy_warnings: list[str] = []
    if policy_status == "failed":
        policy_warnings.append("Policy check failed — see validator logs for details.")
    elif policy_status == "not_run" and "policy" in contract.get("required_validators", []):
        policy_warnings.append("Policy validator was required but has not been recorded.")

    # Build validator result summary.
    validator_results = [
        {
            "validator": r.get("validator"),
            "status": r.get("status"),
            "exit_code": r.get("exit_code"),
            "summary": r.get("summary"),
            "log_path": r.get("log_path"),
            "created_at": r.get("created_at"),
        }
        for r in validation_results
    ]

    return {
        "task_key": task_key,
        "mission_contract": contract,
        "artifacts": files,
        "validator_results": validator_results,
        "policy_status": policy_status,
        "policy_warnings": policy_warnings,
    }


__all__ = [
    "build_artifact_file_summaries",
    "build_artifact_preview",
    "build_contract_summary",
    "build_review_evidence",
]
