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
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_FILENAMES,
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
)


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

_VALIDATOR_STATUS_ORDER = {
    "failed": 0,
    "blocked": 1,
    "not_run": 2,
    "unknown": 3,
    "not_required": 4,
    "passed": 5,
}

# Fallback for validators with no status in the ordering map.
_FALLBACK_ORDER = 99

_CONTRACT_NAME = "mission_contract.json"

_EVIDENCE_CATEGORIES = (
    "issue",
    "workspace",
    "execution",
    "validation",
    "review",
    "handoff",
    "publication",
    "draft_pr",
    "preflight",
    "governance",
    "other",
)

_EVIDENCE_SAFETY = {
    "read_only": True,
    "push_available_from_this_endpoint": False,
    "pr_creation_available_from_this_endpoint": False,
    "merge_available_from_this_endpoint": False,
    "cleanup_available_from_this_endpoint": False,
    "approval_available_from_this_endpoint": False,
}


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
    if name in WORKFLOW_POLICY_ARTIFACT_FILENAMES:
        return WORKFLOW_POLICY_REVIEW_KIND
    if name.startswith("pi-") or name.startswith("opencode-"):
        return "executor_log"
    return "other"


def _scan_for_secrets(text: str) -> bool:
    """Return True if text contains high-confidence secret assignments."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _unavailable_workflow_policy_evidence() -> dict[str, Any]:
    return {
        "available": False,
        "artifact_index": None,
        "summary": None,
        "review_artifacts": [],
    }


def _is_safe_relative_artifact_path(raw_path: Any) -> bool:
    if not isinstance(raw_path, str):
        return False
    value = raw_path.strip().replace("\\", "/")
    if not value or value.startswith("/") or value in {".", ".."}:
        return False
    if ".." in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _valid_artifact_index_entries(artifacts: Any) -> bool:
    if not isinstance(artifacts, list):
        return False

    has_summary_entry = False
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return False

        name = artifact.get("name")
        artifact_type = artifact.get("artifact_type")
        path = artifact.get("path")
        required = artifact.get("required")

        if not isinstance(name, str) or not name.strip():
            return False
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            return False
        if not _is_safe_relative_artifact_path(path):
            return False
        if not isinstance(required, bool):
            return False

        if (
            name == WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE
            and artifact_type == WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE
            and path == WORKFLOW_POLICY_SUMMARY_FILENAME
            and required is True
        ):
            has_summary_entry = True

    return has_summary_entry


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


def _artifact_category(
    *,
    artifact_type: str | None = None,
    name: str | None = None,
    kind: str | None = None,
) -> str:
    normalized_type = (artifact_type or "").strip().lower()
    normalized_name = (name or "").strip().lower()
    normalized_kind = (kind or "").strip().lower()

    if normalized_type in {"issue_spec", "spec"} or normalized_name in {
        "issue_spec.md",
        "issue_spec.json",
    }:
        return "issue"
    if normalized_type == "pr_handoff" or normalized_name.startswith("pr_handoff"):
        return "handoff"
    if normalized_type == "branch_push" or normalized_name.startswith("branch_push"):
        return "publication"
    if normalized_type == "draft_pr" or normalized_name.startswith("draft_pr"):
        return "draft_pr"
    if normalized_type == "preflight" or "preflight" in normalized_name:
        return "preflight"
    if normalized_kind == WORKFLOW_POLICY_REVIEW_KIND or normalized_type.startswith(
        "workflow_policy"
    ):
        return "governance"
    if normalized_kind == "mission_contract" or normalized_name == _CONTRACT_NAME:
        return "execution"
    if normalized_kind == "executor_log" or normalized_type in {
        "worker_log",
        "manifest",
    }:
        return "execution"
    if normalized_kind == "validator_log":
        return "validation"
    if normalized_type in {"review_log", "decision"}:
        return "review"
    return "other"


def _is_artifact_dir_child(artifact_dir: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(artifact_dir.resolve())
    except (OSError, ValueError):
        return False
    return True


def _db_artifact_evidence_item(
    artifact: Any,
    *,
    artifact_dir: Path,
    file_summaries_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    path = Path(artifact.path)
    name = path.name
    summary = file_summaries_by_name.get(name) if _is_artifact_dir_child(artifact_dir, path) else None
    kind = summary.get("kind", "artifact_record") if summary else "artifact_record"
    return {
        "name": name,
        "artifact_type": artifact.artifact_type,
        "kind": kind,
        "category": _artifact_category(
            artifact_type=artifact.artifact_type,
            name=name,
            kind=kind,
        ),
        "path": str(path),
        "exists": path.exists(),
        "preview_available": bool(summary and summary.get("preview_available")),
        "size_bytes": summary.get("size_bytes") if summary else (path.stat().st_size if path.is_file() else 0),
        "source": "artifact_record",
        "created_at": artifact.created_at,
    }


def _file_evidence_item(
    summary: dict[str, Any],
    *,
    artifact_dir: Path,
) -> dict[str, Any]:
    name = summary["name"]
    return {
        "name": name,
        "artifact_type": summary["kind"],
        "kind": summary["kind"],
        "category": _artifact_category(name=name, kind=summary["kind"]),
        "path": str(artifact_dir / name),
        "exists": True,
        "preview_available": summary["preview_available"],
        "size_bytes": summary["size_bytes"],
        "source": "artifact_directory",
        "has_secret_warning": summary["has_secret_warning"],
        "is_binary": summary["is_binary"],
    }


def _validation_evidence_item(result: dict[str, Any]) -> dict[str, Any]:
    validator = result.get("validator")
    status = result.get("status")
    log_path = result.get("log_path")
    return {
        "name": str(validator or "validation"),
        "artifact_type": "validation_result",
        "kind": "validation_result",
        "category": "validation",
        "path": str(log_path) if log_path else None,
        "exists": Path(log_path).exists() if log_path else False,
        "preview_available": False,
        "size_bytes": Path(log_path).stat().st_size if log_path and Path(log_path).is_file() else 0,
        "source": "validation_result",
        "validator": validator,
        "status": status,
        "exit_code": result.get("exit_code"),
        "summary": result.get("summary"),
        "created_at": result.get("created_at"),
    }


def _empty_evidence_categories() -> dict[str, list[dict[str, Any]]]:
    return {category: [] for category in _EVIDENCE_CATEGORIES}


# ----------------------------------------------------------------------
# Validators helpers
# ----------------------------------------------------------------------


def _latest_validator_result(
    results: list[dict[str, Any]],
    validator: str,
) -> dict[str, Any] | None:
    """Return the latest result for the given validator.

    Results are expected to be in insertion order (oldest first). The latest
    result for a given validator is the LAST one in the list with that validator
    name — this is stable regardless of identical timestamps.
    """
    # Iterate in reverse to find the last occurrence of this validator.
    # This is equivalent to max(results, key=position_in_list) where position
    # is counted from the end. We walk the list in reverse and return the first
    # match, which is the most-recently-inserted result for this validator.
    latest: dict[str, Any] | None = None
    for r in reversed(results):
        if r.get("validator") == validator:
            latest = r
            break
    return latest


def _sorted_validator_results(
    results: list[dict[str, Any]],
    validator: str,
) -> list[dict[str, Any]]:
    """Return validator results sorted newest-first by created_at.

    If timestamps are identical (same-second recording), secondary sort by
    id() provides stable insertion-order determinism across test runs.
    """
    filtered = [r for r in results if r.get("validator") == validator]
    return sorted(
        filtered,
        key=lambda r: (r.get("created_at", ""), id(r)),
        reverse=True,
    )


def _latest_validator_status(
    results: list[dict[str, Any]],
    validator: str,
    default: str = "unknown",
) -> str:
    """Return the status of the latest validator result, or default if none."""
    latest = _latest_validator_result(results, validator)
    if latest is None:
        return default
    return latest.get("status", default)


def _aggregate_policy_status(
    validation_results: list[dict[str, Any]],
    contract: dict[str, Any],
) -> tuple[str, list[str]]:
    """Compute the aggregate policy status and warnings from validation results.

    The aggregate status uses the most recent policy validator result.
    Historical failed results are not counted against the current status.
    """
    required_validators = contract.get("required_validators", [])
    latest = _latest_validator_result(validation_results, "policy")

    if latest is None:
        if "policy" in required_validators:
            return "not_run", ["Policy validator was required but has not been recorded."]
        return "not_required", []

    policy_status = latest.get("status", "unknown")
    policy_warnings: list[str] = []
    if policy_status == "failed":
        policy_warnings.append("Policy check failed — see validator logs for details.")
    elif policy_status == "not_run" and "policy" in required_validators:
        policy_warnings.append("Policy validator was required but has not been recorded.")

    return policy_status, policy_warnings


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
            if _scan_for_secrets(text):
                return {
                    "name": artifact_name,
                    "content": None,
                    "truncated": True,
                    "size_bytes": target_resolved.stat().st_size,
                    "preview_reason": "contains high-confidence secret-like assignment; preview not available",
                }
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

    policy_status, policy_warnings = _aggregate_policy_status(
        validation_results,
        contract,
    )

    # Build validator result summary.
    validator_results = [
        {
            "validator": r.get("validator"),
            "status": r.get("status"),
            "exit_code": r.get("exit_code"),
            "summary": r.get("summary"),
            "log_path": r.get("log_path"),
            "artifacts": r.get("artifacts", {}),
            "created_at": r.get("created_at"),
        }
        for r in validation_results
    ]

    # Read-only workflow policy evidence from existing artifacts.
    workflow_policy_evidence = build_workflow_policy_evidence(artifact_dir)

    return {
        "task_key": task_key,
        "mission_contract": contract,
        "artifacts": files,
        "validator_results": validator_results,
        "policy_status": policy_status,
        "policy_warnings": policy_warnings,
        "workflow_policy_evidence": workflow_policy_evidence,
    }


def build_task_evidence_readback(
    *,
    task_key: str,
    artifact_dir: Path,
    task_artifacts: list[Any],
    validation_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build grouped read-only dogfood evidence from existing task evidence.

    This summary reads existing task artifact records, artifact directory files,
    and validation rows. It does not create records, parse large artifacts, run
    validators, call executors, dispatch tasks, or mutate external systems.
    """
    categories = _empty_evidence_categories()
    file_summaries = build_artifact_file_summaries(artifact_dir)
    file_summaries_by_name = {summary["name"]: summary for summary in file_summaries}
    seen_file_names: set[str] = set()

    for artifact in task_artifacts:
        item = _db_artifact_evidence_item(
            artifact,
            artifact_dir=artifact_dir,
            file_summaries_by_name=file_summaries_by_name,
        )
        categories[item["category"]].append(item)
        if _is_artifact_dir_child(artifact_dir, Path(artifact.path)):
            seen_file_names.add(Path(artifact.path).name)

    for summary in file_summaries:
        if summary["name"] in seen_file_names:
            continue
        item = _file_evidence_item(summary, artifact_dir=artifact_dir)
        categories[item["category"]].append(item)

    for result in validation_results:
        item = _validation_evidence_item(result)
        categories["validation"].append(item)

    validation_statuses = [
        {
            "validator": result.get("validator"),
            "status": result.get("status"),
            "summary": result.get("summary"),
        }
        for result in validation_results
    ]

    def has_category(category: str) -> bool:
        return bool(categories.get(category))

    return {
        "task_key": task_key,
        "available": True,
        "categories": categories,
        "summary": {
            "has_issue_spec": has_category("issue"),
            "has_pr_handoff": has_category("handoff"),
            "has_branch_push": has_category("publication"),
            "has_draft_pr": has_category("draft_pr"),
            "has_preflight": has_category("preflight"),
            "validation_statuses": validation_statuses,
        },
        "safety": dict(_EVIDENCE_SAFETY),
    }


def build_workflow_policy_evidence(
    artifact_dir: Path,
) -> dict[str, Any]:
    """Build a read-only workflow_policy_evidence block from existing artifacts.

    This function reads the workflow_policy_summary.json and
    artifact_index.json files if they exist. It does NOT generate,
    validate, or mutate those files. It does NOT call the dispatcher,
    executors, validators, or any write operations.

    Strict availability: available=true only when both canonical files exist,
    parse as valid JSON, and meet canonical contract requirements. Otherwise
    returns available=False without masking missing or corrupt content.

    Returns:
        {
            "available": bool,
            "artifact_index": {...} | None,
            "summary": {...} | None,
            "review_artifacts": list,
        }
    """
    summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
    index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME

    summary_exists = summary_path.exists()
    index_exists = index_path.exists()

    if not summary_exists or not index_exists:
        return _unavailable_workflow_policy_evidence()

    # Read and parse the index artifact.
    index_data: dict[str, Any] | None = None
    try:
        index_text = index_path.read_text(encoding="utf-8", errors="replace")
        index_data = json.loads(index_text)
    except (OSError, json.JSONDecodeError):
        pass

    if index_data is None:
        return _unavailable_workflow_policy_evidence()

    # Read and parse the summary artifact.
    summary_data: dict[str, Any] | None = None
    try:
        summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
        summary_data = json.loads(summary_text)
    except (OSError, json.JSONDecodeError):
        pass

    if summary_data is None:
        return _unavailable_workflow_policy_evidence()

    # --- Strict index validation ---
    if index_data.get("package_type") != WORKFLOW_POLICY_PACKAGE_TYPE:
        return _unavailable_workflow_policy_evidence()
    if index_data.get("artifact_index_version") != WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION:
        return _unavailable_workflow_policy_evidence()
    if not _valid_artifact_index_entries(index_data.get("artifacts")):
        return _unavailable_workflow_policy_evidence()

    # --- Strict summary validation ---
    if summary_data.get("artifact_type") != WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE:
        return _unavailable_workflow_policy_evidence()
    if not summary_data.get("validation_status"):
        return _unavailable_workflow_policy_evidence()

    # Verify all required summary fields are present.
    for field in WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS:
        if field not in summary_data:
            return _unavailable_workflow_policy_evidence()

    # governance_invariants must be dict or list when present.
    gi = summary_data.get("governance_invariants")
    if gi is not None and not isinstance(gi, (dict, list)):
        return _unavailable_workflow_policy_evidence()

    # --- Build artifact_index section from verified content ---
    artifact_index_section: dict[str, Any] = {
        "name": WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
        "artifact_type": WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
        "path": WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
        "package_type": index_data["package_type"],
        "artifact_index_version": index_data["artifact_index_version"],
        "generated_at": index_data.get("generated_at", ""),
        "artifacts": index_data["artifacts"],
    }

    # --- Build summary section from verified content (no fallbacks) ---
    summary_section: dict[str, Any] = {
        "name": WORKFLOW_POLICY_SUMMARY_FILENAME,
        "artifact_type": summary_data["artifact_type"],
        "path": WORKFLOW_POLICY_SUMMARY_FILENAME,
        "schema_version": summary_data["schema_version"],
        "validation_status": summary_data["validation_status"],
        "validation_errors": summary_data["validation_errors"],
        "validation_warnings": summary_data["validation_warnings"],
        "source_path": summary_data["source_path"],
        "generated_at": summary_data["generated_at"],
        "allowed_executors": summary_data["allowed_executors"],
        "required_validators": summary_data["required_validators"],
        "optional_validators": summary_data.get("optional_validators", []),
        "path_policy": summary_data["path_policy"],
        "workspace_policy": summary_data["workspace_policy"],
        "proof_of_work": summary_data["proof_of_work"],
        "human_review": summary_data["human_review"],
        "forbidden_actions": summary_data["forbidden_actions"],
        "deferred_integrations": summary_data["deferred_integrations"],
        "governance_invariants": summary_data["governance_invariants"],
    }

    # --- Build review_artifacts from artifact directory scan ---
    all_files = build_artifact_file_summaries(artifact_dir)
    review_artifacts = [
        f for f in all_files if f["kind"] == WORKFLOW_POLICY_REVIEW_KIND
    ]

    return {
        "available": True,
        "artifact_index": artifact_index_section,
        "summary": summary_section,
        "review_artifacts": review_artifacts,
    }


__all__ = [
    "build_artifact_file_summaries",
    "build_artifact_preview",
    "build_contract_summary",
    "build_review_evidence",
    "build_task_evidence_readback",
    "build_workflow_policy_evidence",
]
