"""Read-only Attempt snapshots and immutable managed-launch observations.

These artifacts index evidence; they never authorize a launch, signal, or outcome.
The caller supplies the existing preflight result and process-start observation.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import utc_now_iso

if TYPE_CHECKING:
    from agent_taskflow.executor_launch import (
        ExecutorLaunchBinding,
        ExecutorLaunchPreflightResult,
        ExecutorLaunchSpec,
    )

_CONFIG_FIELDS = (
    "executor", "model", "base_commit", "policy_version", "config_snapshot_hash",
    "prompt_template_version", "permission_profile",
)


def read_bound_attempt_snapshot(binding: ExecutorLaunchBinding) -> dict[str, Any]:
    """Read selected configuration only after exact identity/path verification.

    Opening mode=ro avoids implicit creation, migrations, and store initialization.
    Both queries use one read transaction, with no latest-Attempt lookup or fallback.
    """
    snapshot: dict[str, Any] = {
        "configured": dict.fromkeys(_CONFIG_FIELDS),
        "attempt_number": None,
        "provenance": {
            "source": "persisted_attempt", "read_only": True,
            "binding_verified": False, "status": "unavailable", "error_type": None,
            "read_at": utc_now_iso(),
        },
    }
    provenance = snapshot["provenance"]
    try:
        with closing(sqlite3.connect(binding.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT a.attempt_number, a.worktree_path, a.artifact_root,
                       r.worktree_path AS resource_worktree, r.artifact_root AS resource_artifacts
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                JOIN runtime_leases l ON l.attempt_id = a.attempt_id AND l.task_id = a.task_id
                JOIN attempt_resources r ON r.attempt_id = a.attempt_id AND r.task_id = a.task_id
                    AND r.task_key = t.task_key AND r.attempt_number = a.attempt_number
                    AND r.owner_id = l.owner_id
                WHERE a.attempt_id = ? AND a.task_id = ? AND t.task_key = ?
                    AND l.lease_id = ? AND l.owner_id = ?
                """,
                (binding.attempt_id, binding.task_id, binding.task_key,
                 binding.lease_id, binding.owner_id),
            ).fetchone()
            if row is None or any(
                row[column] is None or Path(row[column]).resolve() != expected.resolve()
                for column, expected in (
                    ("worktree_path", binding.worktree_path),
                    ("artifact_root", binding.artifact_root),
                    ("resource_worktree", binding.worktree_path),
                    ("resource_artifacts", binding.artifact_root),
                )
            ):
                provenance["status"] = "binding_mismatch"
                return snapshot
            provenance["binding_verified"] = True
            snapshot["attempt_number"] = row["attempt_number"]
            config = conn.execute(
                "SELECT " + ", ".join(_CONFIG_FIELDS) + " FROM attempts WHERE attempt_id = ?",
                (binding.attempt_id,),
            ).fetchone()
            snapshot["configured"] = dict(config)
            provenance["status"] = "read"
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        # Error strings may contain paths or query data; persist only the type.
        provenance["error_type"] = type(exc).__name__
    return snapshot


def launch_evidence_reference(artifact_root: Path, process_id: str) -> dict[str, Any]:
    if re.fullmatch(r"process-[a-f0-9]{32}", process_id) is None:
        raise ValueError("invalid managed process identity")
    return {
        "path": str(artifact_root / f"resolved-launch-{process_id}.json"),
        "status": "pending", "error_type": None,
    }


def _open_directory_without_symlinks(root: Path) -> int:
    """Anchor publication to the actual directory, rejecting symlink traversal."""
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("evidence root must be an absolute path without traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _verify_directory_reference(root: Path, directory: int) -> None:
    current = _open_directory_without_symlinks(root)
    try:
        if not _same_inode(os.fstat(current), os.fstat(directory)):
            raise ValueError("artifact directory reference changed during publication")
    finally:
        os.close(current)


def _verify_regular_file(fd: int, expected: bytes, identity: os.stat_result | None = None) -> os.stat_result:
    observed = os.fstat(fd)
    if not stat.S_ISREG(observed.st_mode) or (identity is not None and not _same_inode(observed, identity)):
        raise ValueError("launch evidence file identity or type changed")
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as handle:
        if handle.read(len(expected) + 1) != expected:
            raise ValueError("launch evidence content differs from runner payload")
    return observed


def _verify_file_reference(directory: int, name: str, expected: bytes, identity: os.stat_result) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        _verify_regular_file(fd, expected, identity)
        if not _same_inode(os.stat(name, dir_fd=directory, follow_symlinks=False), identity):
            raise ValueError("launch evidence file reference changed")
    finally:
        os.close(fd)


def _publish_once(root: Path, name: str, payload: dict[str, Any]) -> None:
    """Publish verified bytes from a pinned regular file, without overwrite.

    Check pathname/inode correspondence around publication. This detects observed
    publication races; it does not provide isolation from arbitrary same-UID writes.
    """
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("evidence filename must be one path component")
    expected = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    directory = _open_directory_without_symlinks(root)
    staged_name = f".{name}.{uuid4().hex}.staged"
    staged_fd: int | None = None
    published = False
    identity: os.stat_result | None = None
    try:
        atomic_write_json(Path(f"/proc/self/fd/{directory}") / staged_name,
                          payload, sort_keys=True)
        _verify_directory_reference(root, directory)
        staged_fd = os.open(staged_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        identity = _verify_regular_file(staged_fd, expected)
        _verify_file_reference(directory, staged_name, expected, identity)
        # Follow only the kernel's descriptor symlink, never the mutable staging name.
        os.link(f"/proc/self/fd/{staged_fd}", name, dst_dir_fd=directory, follow_symlinks=True)
        published = True
        _verify_file_reference(directory, staged_name, expected, identity)
        _verify_file_reference(directory, name, expected, identity)
        try:
            os.fsync(directory)
        except OSError:
            pass  # Same best-effort durability rule as the shared atomic writer.
        _verify_directory_reference(root, directory)
        _verify_file_reference(directory, name, expected, identity)
    except BaseException:
        if published and identity is not None:
            try:
                # Remove only the new link to our own inode, never a colliding artifact.
                if _same_inode(os.stat(name, dir_fd=directory, follow_symlinks=False), identity):
                    os.unlink(name, dir_fd=directory)
            except OSError:
                pass
        raise
    finally:
        if staged_fd is not None:
            os.close(staged_fd)
        try:
            os.unlink(staged_name, dir_fd=directory)
        except OSError:
            pass
        os.close(directory)


def write_launch_evidence(
    binding: ExecutorLaunchBinding,
    spec: ExecutorLaunchSpec,
    *,
    process_id: str,
    snapshot: dict[str, Any],
    preflight: ExecutorLaunchPreflightResult,
    preflight_started_at: str,
    preflight_ended_at: str,
    launch_spec_path: Path,
    pid_manifest_path: Path,
    outcome: str,
    parent_environment_inherited_by_popen: bool,
    process_identity: dict[str, int] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write once per process, reporting evidence failure without altering lifecycle."""
    reference = launch_evidence_reference(binding.artifact_root, process_id)
    reference["metadata_provenance"] = snapshot["provenance"]
    if not snapshot["provenance"]["binding_verified"]:
        reference["status"] = "not_written"
        return reference  # Never write to a merely claimed, unverified artifact root.

    def redact(message: str) -> str:
        for index in spec.redacted_arg_indexes:
            message = message.replace(spec.argv[index], "<redacted>")
        return message

    configured = snapshot["configured"]
    executable_redacted = 0 in spec.redacted_arg_indexes
    observed_executable = None
    if process_identity is not None and not executable_redacted:
        try:
            observed_executable = redact(os.readlink(f"/proc/{process_identity['pid']}/exe"))
        except OSError:
            pass  # A fast process may already have exited; do not infer its executable.
    unknown = {
        "observed_model": None,
        "canonical_execution_path": None,
        "prompt_reference": None,
        "spec_reference": None,
        "config_snapshot_reference": None,
        "allowed_tools": None,
        "environment_allowlist": None,
        "network_policy": None,
    }
    payload = {
        "schema_version": "resolved_launch_evidence.v1",
        "task_id": binding.task_id, "task_key": binding.task_key,
        "attempt_id": binding.attempt_id, "attempt_number": snapshot["attempt_number"],
        "process_id": process_id, "process_role": spec.process_role,
        "lease_id": binding.lease_id, "owner_id": binding.owner_id,
        "recorded_at": utc_now_iso(), "launch_outcome": outcome,
        "process_identity": process_identity, "start_error": error,
        "observed_process_executable": observed_executable,
        "observed_process_executable_provenance": (
            "redacted_argv0" if executable_redacted else
            "linux_proc_exe" if observed_executable is not None else "not_observed"
        ),
        "configured_attempt": configured,
        "model_provenance": "configured_attempt_model_only; runtime_model_not_attested",
        "metadata_provenance": snapshot["provenance"],
        "launch_boundary": "run_managed_process",
        "unknown_field_provenance": "not_attested_at_managed_launch_boundary",
        **unknown,
        "missing_fields": [*unknown, *(
            f"configured_attempt.{key}" for key, value in configured.items() if value is None
        ), *(["observed_process_executable"] if observed_executable is None else []),
            *(["preflight.resolved_executable"] if executable_redacted else [])],
        "resolved_launch_spec": spec.to_artifact(binding),
        "preflight": {
            "observed": True, "ok": preflight.ok,
            "started_at": preflight_started_at, "ended_at": preflight_ended_at,
            "resolved_executable": (redact(preflight.resolved_executable)
                                    if preflight.resolved_executable and not executable_redacted else None),
            "resolved_executable_provenance": (
                "redacted_argv0" if executable_redacted else
                "preflight_resolution" if preflight.resolved_executable else "not_resolved"
            ),
            "blocking_errors": [redact(value) for value in preflight.blocking_errors],
            "warnings": [redact(value) for value in preflight.warnings],
        },
        "environment_inheritance_retained": True,
        "parent_environment_inherited_by_popen": parent_environment_inherited_by_popen,
        "environment_source": (
            "parent_environment" if parent_environment_inherited_by_popen else "caller_supplied_mapping"
        ),
        "environment_keys_are_allowlist": False,
        "network_isolation": False,
        "security_eligibility_established": False,
        "legacy_launch_spec_path": str(launch_spec_path),
        "legacy_pid_manifest_path": str(pid_manifest_path),
        "legacy_references_are_mutable": True,
        "lifecycle_authority": False,
    }
    try:
        _publish_once(binding.artifact_root, Path(reference["path"]).name, payload)
    except (OSError, ValueError, TypeError) as exc:
        reference.update(status="write_failed", error_type=type(exc).__name__)
    else:
        reference["status"] = "written"
    return reference
