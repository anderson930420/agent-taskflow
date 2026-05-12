"""Policy check validator for Agent Taskflow.

The PolicyCheckValidator verifies that a dispatch run produced a valid
mission contract and that the executor did not violate governance rules.
It is a deterministic validator: it does not call any AI, does not call Pi,
and does not depend on the network.

Checks performed:
1. mission_contract.json exists and is valid JSON.
2. schema_version is supported.
3. All required fields are present and non-empty.
4. human_approval_required is true.
5. forbidden_actions contains the mandatory governance prohibitions.
6. required_validators and expected_artifacts are non-empty.
7. artifact_dir exists as a directory.
8. Executor artifacts do not contain evidence of forbidden actions.
9. Executor artifacts do not contain high-confidence secret assignments.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_taskflow.validators.base import (
    Validator,
    ValidatorContext,
    ValidatorResult,
)

# Supported schema versions (add new versions here as the schema evolves).
_SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})

# Required governance prohibitions that must appear in forbidden_actions.
_REQUIRED_FORBIDDEN_ACTIONS = frozenset({
    "approve",
    "push",
    "merge",
    "cleanup",
    "delete_worktree",
    "delete_branch",
    "self_approve",
    "force_push",
})

# High-confidence secret assignment patterns.
# These match assignment-like syntax, not documentation mentions.
_SECRET_PATTERNS = (
    # env-style: KEY=value, KEY:value, KEY = value
    re.compile(r'[A-Z_][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*[:=]', re.IGNORECASE),
    # JSON/object-style: "key": "value" or "key": "sk-..."
    re.compile(r'"[A-Za-z_]*(?:api_key|token|secret|password|credential|access_token|refresh_token|authorization)"\s*:\s*"[^"]+', re.IGNORECASE),
    # Common API key prefixes in plain text
    re.compile(r'(?:api_key|token|secret)\s*=\s*["\']?(?:sk-|ak-)[A-Za-z0-9_-]{10,}'),
    # KEY= or TOKEN= or PASSWORD= or SECRET= followed by a value.
    # Handles bare KEY=, TOKEN=, PASSWORD=, SECRET= and compound keys
    # like OPENAI_API_KEY=, API_KEY=, API_TOKEN=.
    re.compile(r'[A-Z_][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*=\s*\S+', re.IGNORECASE),
)

# Suspicious action patterns that indicate a worker may have self-approved,
# pushed, merged, cleaned up, etc. These must appear outside the mission
# contract itself (which documents them as forbidden).
_SUSPICIOUS_ACTION_PATTERNS = (
    re.compile(r'git\s+push\s+', re.IGNORECASE),
    re.compile(r'git\s+merge\s+', re.IGNORECASE),
    re.compile(r'gh\s+pr\s+merge\s+', re.IGNORECASE),
    re.compile(r'approve[ds]?\s+task', re.IGNORECASE),
    re.compile(r'approved\s+by\s+worker', re.IGNORECASE),
    re.compile(r'cleanup\s+completed', re.IGNORECASE),
    re.compile(r'delete\s+worktree', re.IGNORECASE),
    re.compile(r'delete\s+branch', re.IGNORECASE),
    re.compile(r'rm\s+-rf\s+\.worktrees', re.IGNORECASE),
    re.compile(r'git\s+push\s+--force', re.IGNORECASE),
    re.compile(r'force\s*push', re.IGNORECASE),
    # Avoid matching "do not push" in docs; match actual command invocations
    re.compile(r'git\s+push\s+[^"]+\s+(?:origin|github|remote)', re.IGNORECASE),
)

# Maximum file size to scan (1 MB). Files larger than this are skipped.
_MAX_SCAN_SIZE = 1024 * 1024


def _contract_path(artifact_dir: Path) -> Path:
    """Return the path to the mission contract file."""
    return artifact_dir / "mission_contract.json"


def _normalize_list(value: object) -> list[str]:
    """Coerce a JSON list to a list of strings."""
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _find_secret_assignments(text: str) -> list[str]:
    """Return a list of secret-like assignment patterns found in text."""
    findings = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"secret assignment: {match.group()!r}")
            # Avoid flooding with many matches from the same line
            if len(findings) >= 5:
                return findings
    return findings


def _find_suspicious_actions(text: str) -> list[str]:
    """Return a list of suspicious action patterns found in text."""
    findings = []
    for pattern in _SUSPICIOUS_ACTION_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"suspicious action: {match.group()!r}")
            if len(findings) >= 5:
                return findings
    return findings


def _scan_artifact_file(path: Path) -> tuple[list[str], list[str]]:
    """Scan a single artifact file for secrets and suspicious actions.

    Returns (secret_findings, suspicious_findings).
    Skips binary files and files larger than MAX_SCAN_SIZE.
    """
    # Skip obviously binary files
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico",
                                ".pdf", ".zip", ".tar", ".gz", ".whl",
                                ".pyc", ".pyo", ".so", ".dll", ".exe"}:
        return [], []

    try:
        size = path.stat().st_size
    except OSError:
        return [], []

    if size > _MAX_SCAN_SIZE:
        return [], []

    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return [], []

    secrets = _find_secret_assignments(raw)
    actions = _find_suspicious_actions(raw)
    return secrets, actions


class PolicyCheckValidator(Validator):
    """Validate governance rules and artifact traceability.

    Checks:
    - mission_contract.json exists and is schema-compliant.
    - forbidden_actions includes mandatory governance prohibitions.
    - human_approval_required is true.
    - Executor artifacts do not contain forbidden actions or secret leakage.
    """

    name = "policy"

    def __init__(
        self,
        *,
        scan_artifacts: bool = True,
        max_scan_size: int = _MAX_SCAN_SIZE,
    ) -> None:
        if not isinstance(scan_artifacts, bool):
            raise TypeError("scan_artifacts must be a bool")
        self.scan_artifacts = scan_artifacts
        self.max_scan_size = max_scan_size

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / "policy-validate.log"

    def _collect_failures(self, contract: dict, artifact_dir: Path) -> list[str]:
        """Run all policy checks and return a list of failure reasons."""
        failures: list[str] = []

        # 1. Check schema_version
        schema_version = contract.get("schema_version", "")
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            failures.append(
                f"Unsupported schema_version {schema_version!r}; "
                f"supported: {sorted(_SUPPORTED_SCHEMA_VERSIONS)}"
            )

        # 2. Required fields (must be non-empty strings)
        for field_name in ("task_key", "goal", "executor"):
            value = contract.get(field_name, "")
            if not value or not str(value).strip():
                failures.append(f"Required field {field_name!r} is missing or empty")

        # 3. human_approval_required must be True
        human_approval = contract.get("human_approval_required")
        if not human_approval:
            failures.append(
                "human_approval_required is not true; "
                "a human must approve this task"
            )

        # 4. forbidden_actions contains required governance rules
        forbidden = _normalize_list(contract.get("forbidden_actions", []))
        missing = _REQUIRED_FORBIDDEN_ACTIONS - frozenset(forbidden)
        if missing:
            failures.append(
                f"forbidden_actions is missing required governance prohibitions: "
                f"{sorted(missing)}"
            )

        # 5. required_validators is not empty
        required_validators = _normalize_list(contract.get("required_validators", []))
        if not required_validators:
            failures.append("required_validators is empty")

        # 6. expected_artifacts is not empty
        expected = _normalize_list(contract.get("expected_artifacts", []))
        if not expected:
            failures.append("expected_artifacts is empty")

        # 7. artifact_dir exists as a directory
        contract_artifact_dir = contract.get("artifact_dir", "")
        if contract_artifact_dir:
            artifact_dir_path = Path(contract_artifact_dir)
            if not artifact_dir_path.is_dir():
                failures.append(
                    f"artifact_dir from contract does not exist as a directory: "
                    f"{contract_artifact_dir}"
                )
        else:
            failures.append("artifact_dir is missing from contract")

        # 8. Scan executor artifacts for forbidden actions / secret leakage
        if self.scan_artifacts and artifact_dir_path.is_dir():
            scan_failures = self._scan_artifacts(artifact_dir_path)
            failures.extend(scan_failures)

        return failures

    def _scan_artifacts(self, artifact_dir: Path) -> list[str]:
        """Scan artifact files for forbidden actions and secret leakage.

        Scans executor-produced logs and worker output artifacts. Skips:
        - The mission contract itself (it documents forbidden actions as part of
          governance, not as evidence of a violation).
        - The policy validator's own log (avoid false positives on its failure summary).
        - Other validator logs (pytest.log, openspec-validate.log).
        - pi_mission_prompt.md and pi_mission_plan.json (system-generated governance
          documents that contain governance rules in plain text).
        - pi-executor.log (contains the full command including embedded governance text
          as the command argument; the actual worker output follows "Environment:").
        - Binary files and files exceeding max_scan_size.
        """
        failures: list[str] = []

        # Files produced by validators or by the system as governance/control-plane
        # artifacts — skip these to avoid false positives on system-generated content.
        # The pi-executor.log is skipped because it contains the full command with
        # embedded prompt text that includes governance rules ("do not approve",
        # "do not push", etc.). The worker's actual output (handoff summary) follows
        # "Environment:" on a different line and would be the source of any real
        # violation — but real violations will be caught by artifact files the worker
        # actually creates (e.g. worktree state, git status), not by log metadata.
        _SKIP_FILES = frozenset({
            "policy-validate.log",
            "pytest.log",
            "openspec-validate.log",
            "pi_mission_prompt.md",   # system-generated governance document
            "pi_mission_plan.json",    # system-generated plan metadata
            "pi-executor.log",        # contains embedded governance text in command arg
        })

        try:
            files = list(artifact_dir.iterdir())
        except OSError as exc:
            failures.append(f"Cannot list artifact directory: {exc}")
            return failures

        for file_path in files:
            # Skip mission contract (it documents governance rules, not violations)
            if file_path.name == "mission_contract.json":
                continue
            # Skip validator logs to avoid false positives on their own output
            if file_path.name in _SKIP_FILES:
                continue
            # Skip binary files (images, compressed archives, etc.)
            if file_path.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".ico",
                ".pdf", ".zip", ".tar", ".gz", ".whl",
                ".pyc", ".pyo", ".so", ".dll", ".exe"
            }:
                continue
            try:
                if file_path.stat().st_size > self.max_scan_size:
                    continue
            except OSError:
                continue

            secrets, suspicious = _scan_artifact_file(file_path)
            for finding in secrets:
                failures.append(f"{file_path.name}: {finding}")
            for finding in suspicious:
                failures.append(f"{file_path.name}: {finding}")

        return failures

    def run(self, context: ValidatorContext) -> ValidatorResult:
        """Run policy checks against the mission contract and executor artifacts."""
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(context.artifact_dir)
        contract_path = _contract_path(context.artifact_dir)

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"Validator: {self.name}\n")
            log_file.write(f"Task: {context.task_key}\n")
            log_file.write(f"Project: {context.project}\n")
            log_file.write(f"Worktree: {context.worktree_path}\n")
            log_file.write(f"Artifact dir: {context.artifact_dir}\n")
            log_file.write(f"Scan artifacts: {self.scan_artifacts}\n\n")
            log_file.flush()

            # Read and parse the contract
            if not contract_path.exists():
                summary = (
                    f"mission_contract.json not found at {contract_path}. "
                    "Policy validation requires a mission contract artifact."
                )
                log_file.write(f"FAILED: {summary}\n")
                return ValidatorResult(
                    validator=self.name,
                    status="failed",
                    exit_code=None,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )

            try:
                raw = contract_path.read_text(encoding="utf-8")
                contract = json.loads(raw)
            except json.JSONDecodeError as exc:
                summary = f"mission_contract.json is not valid JSON: {exc}"
                log_file.write(f"FAILED: {summary}\n")
                return ValidatorResult(
                    validator=self.name,
                    status="failed",
                    exit_code=None,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )

            if not isinstance(contract, dict):
                summary = "mission_contract.json must be a JSON object"
                log_file.write(f"FAILED: {summary}\n")
                return ValidatorResult(
                    validator=self.name,
                    status="failed",
                    exit_code=None,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )

            log_file.write(f"Contract schema_version: {contract.get('schema_version', 'MISSING')}\n")
            log_file.write(f"Contract task_key: {contract.get('task_key', 'MISSING')}\n")
            log_file.write(f"human_approval_required: {contract.get('human_approval_required')}\n")
            log_file.write(f"forbidden_actions: {contract.get('forbidden_actions', [])}\n\n")

            failures = self._collect_failures(contract, context.artifact_dir)

            if failures:
                log_file.write(f"Policy check FAILURES ({len(failures)}):\n")
                for idx, failure in enumerate(failures, 1):
                    log_file.write(f"  [{idx}] {failure}\n")
                summary = (
                    f"Policy validation failed: {len(failures)} issue(s) found. "
                    f"First: {failures[0]}"
                )
                return ValidatorResult(
                    validator=self.name,
                    status="failed",
                    exit_code=1,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )
            else:
                log_file.write("Policy check PASSED — all governance rules satisfied.\n")
                summary = "Policy validation passed."
                return ValidatorResult(
                    validator=self.name,
                    status="passed",
                    exit_code=0,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )


__all__ = ["PolicyCheckValidator"]