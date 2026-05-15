"""Changed-files policy validator.

This validator audits git worktree changes against path policy recorded in the
mission contract. It is deterministic and does not call the network or any AI
executor.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.mission_contract import read_mission_contract
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult


AUDIT_ARTIFACT_NAME = "changed-files-audit.json"
LOG_ARTIFACT_NAME = "changed-files-validate.log"


@dataclass(frozen=True)
class ChangedFile:
    """One changed path from git status."""

    path: str
    status: str


def _normalize_policy_paths(raw_paths: object) -> list[str]:
    if not isinstance(raw_paths, list):
        return []

    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            continue
        value = raw.strip().replace("\\", "/").strip("/")
        if not value or value == ".":
            continue
        paths.append(value)
    return paths


def _normalize_changed_path(raw_path: str) -> str:
    return raw_path.strip().replace("\\", "/").strip("/")


def _path_matches_policy(path: str, policy_path: str) -> bool:
    normalized_path = _normalize_changed_path(path)
    normalized_policy = _normalize_changed_path(policy_path)
    return (
        normalized_path == normalized_policy
        or normalized_path.startswith(f"{normalized_policy}/")
    )


def _parse_porcelain_z(raw: str) -> list[ChangedFile]:
    """Parse `git status --porcelain=v1 -z` output."""
    entries = raw.split("\0")
    changed: list[ChangedFile] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            continue

        status = entry[:2]
        path = _normalize_changed_path(entry[3:])
        if not path:
            continue

        changed.append(ChangedFile(path=path, status=status))

        # Rename/copy records in porcelain v1 -z include an extra path field.
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index < len(entries) and entries[index]:
                previous_path = _normalize_changed_path(entries[index])
                if previous_path:
                    changed.append(ChangedFile(path=previous_path, status=status))
            index += 1

    return changed


def collect_changed_files(worktree_path: Path) -> list[ChangedFile]:
    """Return git-tracked and untracked changed files for a worktree."""
    command = [
        "git",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]
    completed = subprocess.run(
        command,
        cwd=worktree_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise RuntimeError(stderr or "git status failed")
    return _parse_porcelain_z(completed.stdout)


class ChangedFilesValidator(Validator):
    """Validate changed files against mission contract path policy."""

    name = "changed-files"

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / LOG_ARTIFACT_NAME

    def _audit_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / AUDIT_ARTIFACT_NAME

    @staticmethod
    def _build_audit(
        *,
        task_key: str,
        worktree_path: Path,
        allowed_paths: list[str],
        forbidden_paths: list[str],
        changed_files: list[ChangedFile],
        collection_error: str | None = None,
    ) -> dict[str, Any]:
        violations: list[dict[str, str]] = []

        for changed in changed_files:
            matched_forbidden = next(
                (
                    forbidden
                    for forbidden in forbidden_paths
                    if _path_matches_policy(changed.path, forbidden)
                ),
                None,
            )
            if matched_forbidden is not None:
                violations.append(
                    {
                        "path": changed.path,
                        "status": changed.status,
                        "reason": "forbidden_path",
                        "policy_path": matched_forbidden,
                    }
                )
                continue

            if allowed_paths and not any(
                _path_matches_policy(changed.path, allowed)
                for allowed in allowed_paths
            ):
                violations.append(
                    {
                        "path": changed.path,
                        "status": changed.status,
                        "reason": "outside_allowed_paths",
                        "policy_path": "",
                    }
                )

        status = "blocked" if collection_error else ("failed" if violations else "passed")
        return {
            "task_key": task_key,
            "worktree_path": str(worktree_path),
            "allowed_paths": allowed_paths,
            "forbidden_paths": forbidden_paths,
            "changed_files": [
                {"path": changed.path, "status": changed.status}
                for changed in changed_files
            ],
            "violations": violations,
            "collection_error": collection_error,
            "status": status,
        }

    def run(self, context: ValidatorContext) -> ValidatorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(context.artifact_dir)
        audit_path = self._audit_path(context.artifact_dir)
        contract_path = context.artifact_dir / "mission_contract.json"

        collection_error: str | None = None
        changed_files: list[ChangedFile] = []
        allowed_paths: list[str] = []
        forbidden_paths: list[str] = []

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"Validator: {self.name}\n")
            log_file.write(f"Task: {context.task_key}\n")
            log_file.write(f"Project: {context.project}\n")
            log_file.write(f"Worktree: {context.worktree_path}\n")
            log_file.write(f"Artifact dir: {context.artifact_dir}\n\n")

            try:
                contract = read_mission_contract(contract_path)
            except (OSError, ValueError, TypeError) as exc:
                collection_error = f"Cannot read mission contract: {exc}"
                log_file.write(f"BLOCKED: {collection_error}\n")
            else:
                allowed_paths = _normalize_policy_paths(contract.get("allowed_paths", []))
                forbidden_paths = _normalize_policy_paths(contract.get("forbidden_paths", []))
                log_file.write(f"allowed_paths: {allowed_paths}\n")
                log_file.write(f"forbidden_paths: {forbidden_paths}\n")

                try:
                    changed_files = collect_changed_files(context.worktree_path)
                except RuntimeError as exc:
                    collection_error = f"Cannot collect changed files: {exc}"
                    log_file.write(f"BLOCKED: {collection_error}\n")
                else:
                    log_file.write(f"changed_files: {len(changed_files)}\n")
                    for changed in changed_files:
                        log_file.write(f"  {changed.status} {changed.path}\n")

            audit = self._build_audit(
                task_key=context.task_key,
                worktree_path=context.worktree_path,
                allowed_paths=allowed_paths,
                forbidden_paths=forbidden_paths,
                changed_files=changed_files,
                collection_error=collection_error,
            )
            audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

            if collection_error is not None:
                return ValidatorResult(
                    validator=self.name,
                    status="blocked",
                    exit_code=None,
                    log_path=log_path,
                    summary=collection_error,
                    artifacts={"log": log_path, "audit": audit_path},
                )

            violations = audit["violations"]
            if violations:
                first = violations[0]
                summary = (
                    "Changed-files validation failed: "
                    f"{len(violations)} path violation(s). "
                    f"First: {first['path']} ({first['reason']})"
                )
                log_file.write(f"FAILED: {summary}\n")
                return ValidatorResult(
                    validator=self.name,
                    status="failed",
                    exit_code=1,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path, "audit": audit_path},
                )

            summary = "Changed-files validation passed."
            log_file.write(f"PASSED: {summary}\n")
            return ValidatorResult(
                validator=self.name,
                status="passed",
                exit_code=0,
                log_path=log_path,
                summary=summary,
                artifacts={"log": log_path, "audit": audit_path},
            )


__all__ = [
    "AUDIT_ARTIFACT_NAME",
    "ChangedFile",
    "ChangedFilesValidator",
    "collect_changed_files",
]
