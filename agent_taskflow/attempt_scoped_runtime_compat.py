"""Compatibility isolation for PR-5 runtime wrapping.

PR-4's public ``CanonicalRuntimeTaskStore`` remains independently testable and
usable for token-admission-only tooling. Runtime entrypoints are upgraded to
``AttemptScopedRuntimeTaskStore`` through the canonicalization hook instead of
replacing the public class symbol globally.

Attempt branch/worktree provisioning is meaningful only for a real Git
repository root. Existing non-Git local/unit workflows retain canonical token
admission without fabricating a branch or pretending a copied directory is a
Git worktree. Level 2 eligibility remains limited to Git-backed projects.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.attempt_scoped_runtime_path import AttemptScopedRuntimeTaskStore


def _is_git_repository_root(path: str | Path) -> bool:
    repo = Path(path)
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo,
            shell=False,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return False
    if completed.returncode != 0:
        return False
    try:
        root = Path(completed.stdout.decode("utf-8", errors="replace").strip()).resolve()
    except (OSError, ValueError):
        return False
    return root == repo.resolve()


def install_attempt_scoped_runtime_compat() -> None:
    """Keep PR-4's class stable while upgrading only runtime entrypoints."""
    if getattr(canonical_path, "__attempt_scoped_compat_installed__", False):
        return

    canonical_store_class = AttemptScopedRuntimeTaskStore.__mro__[1]
    canonical_path.CanonicalRuntimeTaskStore = canonical_store_class

    def canonicalize_store(
        store: Any | None,
        db_path: str | Path | None,
    ) -> AttemptScopedRuntimeTaskStore:
        if isinstance(store, AttemptScopedRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else db_path
        return AttemptScopedRuntimeTaskStore(resolved_path)

    canonical_path._canonicalize_store = canonicalize_store

    original_update = AttemptScopedRuntimeTaskStore.update_task_status

    def update_task_status(
        self: AttemptScopedRuntimeTaskStore,
        task_key: str,
        status: str,
        *,
        message: str | None = None,
        source: str = "local_mirror",
        blocked_reason: str | None = None,
        expected_current_status: str | None = None,
    ) -> None:
        if status == "preparing":
            normalized = canonical_path.normalize_task_key(task_key)
            task = canonical_store_class.get_task(self, normalized)
            if task is not None and not _is_git_repository_root(task.repo_path):
                self._attempt_resource_configs.pop(normalized, None)
                return canonical_store_class.update_task_status(
                    self,
                    normalized,
                    status,
                    message=message,
                    source=source,
                    blocked_reason=blocked_reason,
                    expected_current_status=expected_current_status,
                )
        return original_update(
            self,
            task_key,
            status,
            message=message,
            source=source,
            blocked_reason=blocked_reason,
            expected_current_status=expected_current_status,
        )

    AttemptScopedRuntimeTaskStore.update_task_status = update_task_status
    canonical_path.__attempt_scoped_compat_installed__ = True


__all__ = ["install_attempt_scoped_runtime_compat"]
