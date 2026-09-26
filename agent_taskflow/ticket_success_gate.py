"""Allowlist success for a V1 Ticket's implementation run (RULINGS 67).

OWNER RULING D1: a Ticket reaches ``ready_for_integration`` only when a real
executor invocation happened and exited 0 with ``completed``, every required
validator returned ``passed``, the worktree differs from its base, and the
evidence is present. Anything else — ``skipped``, a dry run, an unknown status,
or merely "not in the failure list" — is a failure.

The dispatcher calls these checks. Each returns ``None`` when its condition
holds, or the reason it does not. They read the database and the worktree and
write nothing, except :func:`control_plane_commit_refusal`, which commits the
executor's output once everything else has passed (OR-10 Q2). They rely on
facts the dispatcher can observe itself, not on what an executor object says
about itself: the managed-launch row that
:func:`agent_taskflow.executor_launch.run_managed_process` records for the
claimed Attempt, its launch spec file, and ``git`` in the worktree.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Iterable

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.execution_policy import ExecutionPolicy

#: Git configuration for the diff check. It must not run repository hooks or a
#: filesystem monitor configured by Ticket-controlled content; the diff itself
#: also passes --no-ext-diff and --no-textconv, so no configured external diff
#: or textconv driver runs either.
_SAFE_GIT = (
    "git",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.quotePath=false",
)
_GIT_TIMEOUT_SECONDS = 60


def argv_sha256(argv: Iterable[str]) -> str:
    """The digest :meth:`ExecutorLaunchSpec.to_artifact` records as ``argv_sha256``."""
    return hashlib.sha256(
        json.dumps(list(argv), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def executor_result_refusal(status: object, exit_code: object) -> str | None:
    """The executor must report ``completed`` and a process exit code of exactly 0."""
    if status != "completed":
        return (
            f"Executor returned {status!r}; a V1 Ticket succeeds only on 'completed' "
            "(skipped, dry-run and unknown results never count)"
        )
    if type(exit_code) is not int or exit_code != 0:
        return f"Executor exit code was {exit_code!r}, not 0; no real invocation exited successfully"
    return None


def managed_invocation_refusal(
    db_path: str | Path,
    attempt_id: str | None,
    policy: ExecutionPolicy,
) -> str | None:
    """Require one verified managed launch of the policy's argv for this Attempt."""
    if not attempt_id:
        return "No claimed Attempt; a real executor invocation cannot be proven"
    try:
        with closing(sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT executor_name, state, exit_code, verified_exit, launch_spec_path
                FROM executor_processes
                WHERE attempt_id = ? AND process_role = 'executor'
                """,
                (attempt_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        return f"Managed launch evidence could not be read: {exc}"
    if len(rows) != 1:
        return (
            f"Expected exactly one managed executor launch for {attempt_id}, found {len(rows)}; "
            "a dry run or an unmanaged executor is not a real invocation"
        )
    row = rows[0]
    if row["executor_name"] != policy.executor:
        return f"Managed launch ran {row['executor_name']!r}, not the policy's {policy.executor!r}"
    if row["state"] != "exited" or row["verified_exit"] != 1 or row["exit_code"] != 0:
        return (
            f"Managed launch ended state={row['state']!r} verified_exit={row['verified_exit']!r} "
            f"exit_code={row['exit_code']!r}; success needs a verified exit 0"
        )
    try:
        spec = json.loads(Path(row["launch_spec_path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return f"Managed launch spec is unreadable: {exc}"
    if not isinstance(spec, dict) or spec.get("attempt_id") != attempt_id:
        return "Managed launch spec is not bound to this Attempt"
    if spec.get("argv_sha256") != argv_sha256(policy.resolved_argv()):
        return "Managed launch argv differs from the execution policy's argv"
    return None


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_SAFE_GIT, *args],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def worktree_diff_refusal(worktree_path: str | Path, base_sha: str | None) -> str | None:
    """Require the worktree to differ from ``base_sha``, committed or not.

    Tracked changes are compared against the base commit, so commits the
    executor made count as well as uncommitted edits; new untracked files that
    are not ignored count too. Nothing is committed here; the control plane
    commits later, in :func:`control_plane_commit_refusal`.
    """
    if not base_sha:
        return "The Attempt has no base commit; a diff against the base cannot be shown"
    worktree = Path(worktree_path)
    try:
        tracked = _git(
            worktree, "diff", "--quiet", "--no-ext-diff", "--no-textconv", base_sha, "--",
        )
        if tracked.returncode == 1:
            return None
        if tracked.returncode != 0:
            return f"git diff against {base_sha} failed: {tracked.stderr.strip()[:500]}"
        untracked = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git could not inspect the worktree: {exc}"
    if untracked.returncode != 0:
        return f"git ls-files failed: {untracked.stderr.strip()[:500]}"
    if untracked.stdout.strip("\0"):
        return None
    return f"The executor produced no change: the worktree does not differ from base {base_sha}"


def _repository_binding_refusal(worktree: Path, repo_path: Path) -> str | None:
    """Require the worktree's git dir to be the one ``repo_path`` registered for it.

    The expected git dir comes from the managed repository (its linked-worktree
    entry whose ``gitdir`` file names this worktree), never from the worktree's
    own ``.git`` file, which the executor can rewrite.
    """
    common = _git(repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        return f"The managed repository {repo_path} is unreadable: {common.stderr.strip()[:500]}"
    common_dir = Path(common.stdout.strip()).resolve()
    registered = [
        entry.resolve() for entry in (common_dir / "worktrees").glob("*")
        if (entry / "gitdir").is_file()
        and Path((entry / "gitdir").read_text(encoding="utf-8").strip()).parent.resolve()
        == worktree.resolve()
    ]
    actual = _git(worktree, "rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir")
    dirs = [Path(line).resolve() for line in actual.stdout.splitlines()]
    if len(registered) != 1 or actual.returncode != 0 or dirs != [registered[0], common_dir]:
        return (
            f"The worktree {worktree} is not bound to the managed repository {repo_path}: "
            f"git dirs {[str(d) for d in dirs]}, expected {[str(d) for d in registered[:1]]} "
            f"and {common_dir}"
        )
    return None


def control_plane_commit_refusal(
    worktree_path: str | Path, base_sha: str | None, message: str, record_path: Path,
    *, repo_path: str | Path, branch: str,
) -> str | None:
    """Commit what the executor left in the worktree, as the control plane (OR-10 Q2).

    The worktree must first be bound to ``repo_path`` (its git dir is the one the
    managed repository registered for it). Everything not ignored is then staged
    and committed with hooks and signing disabled, ``--no-verify``, and
    ``user.useConfigOnly`` so that a missing git identity fails instead of being
    guessed. A clean tree means the executor committed its own change, and
    nothing more is committed. Either way HEAD must then differ from
    ``base_sha``, the tree must be clean, and ``refs/heads/<branch>`` in
    ``repo_path`` must be that HEAD. The resulting HEAD is written to
    ``record_path`` in the Attempt's evidence.
    """
    if not base_sha:
        return "The Attempt has no base commit; its change cannot be committed"
    worktree, repo = Path(worktree_path), Path(repo_path)
    commit = (
        "-c", "user.useConfigOnly=true", "-c", "commit.gpgSign=false",
        "commit", "--no-verify", "-q", "-m", message,
    )
    try:
        binding = _repository_binding_refusal(worktree, repo)
        if binding is not None:
            return binding
        status = _git(worktree, "status", "--porcelain", "-z")
        if status.returncode != 0:
            return f"git status failed: {status.stderr.strip()[:500]}"
        for name, args in (("add", ("add", "-A")), ("commit", commit)) if status.stdout else ():
            step = _git(worktree, *args)
            if step.returncode != 0:
                return f"Control-plane git {name} failed: {step.stderr.strip()[:500]}"
        head = _git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
        after = _git(worktree, "status", "--porcelain", "-z")
        head_sha = head.stdout.strip()
        if head.returncode != 0 or after.returncode != 0 or after.stdout:
            return "The worktree is not clean at a readable HEAD after the control-plane commit"
        if head_sha == base_sha:
            return f"HEAD is still the base {base_sha}; there is no commit to integrate"
        branch_head = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        if branch_head.returncode != 0 or branch_head.stdout.strip() != head_sha:
            return (
                f"refs/heads/{branch} in {repo} is at {branch_head.stdout.strip() or 'nothing'}, "
                f"not the worktree HEAD {head_sha}"
            )
        atomic_write_json(record_path, {
            "base_sha": base_sha, "head_sha": head_sha,
            "committed_by_control_plane": bool(status.stdout),
            "message": message if status.stdout else None,
        }, sort_keys=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"The control-plane commit could not be made: {exc}"
    return None


def validator_status_refusal(name: str, status: object) -> str | None:
    """Every required validator must return exactly ``passed``."""
    if status == "passed":
        return None
    return (
        f"Validator {name} returned {status!r}; a V1 Ticket needs every required "
        "validator to return 'passed'"
    )


def required_evidence_refusal(root: str | Path | None, names: Iterable[str]) -> str | None:
    """Require each named evidence file to exist in the Attempt's artifact root."""
    if root is None:
        return "The Attempt has no artifact root"
    missing = sorted(name for name in names if not (Path(root) / name).is_file())
    if missing:
        return f"Required evidence is missing from {root}: {', '.join(missing)}"
    return None


__all__ = [
    "argv_sha256",
    "control_plane_commit_refusal",
    "executor_result_refusal",
    "managed_invocation_refusal",
    "required_evidence_refusal",
    "validator_status_refusal",
    "worktree_diff_refusal",
]
