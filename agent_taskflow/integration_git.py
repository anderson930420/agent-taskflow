"""Deterministic git operations for the Step 2 integration controller.

Every git invocation goes through :func:`run_git`, which enforces a
subcommand allowlist and a force-push denylist. Nothing here can rewrite
published history or advance the target branch:

* ``--force`` / ``-f`` / ``--force-with-lease`` / ``--force-if-includes`` and
  the ``+refspec`` form are rejected before a push is built or run (§26, §44).
* Pushing the base or a protected branch is rejected, so the target branch can
  never be advanced from here — only a human merging on GitHub advances it
  (§34).

``git merge <target> `` *into a task branch* is allowed and required by §26.
That is the opposite direction from merging a PR, which this layer cannot do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import subprocess
from pathlib import Path
from typing import Callable, Protocol, Sequence


__all__ = [
    "GitRunResult",
    "IntegrationGitError",
    "MergeOutcome",
    "PushOutcome",
    "RebaseOutcome",
    "abort_merge",
    "abort_rebase",
    "assert_no_force_push",
    "behind_count",
    "build_push_command",
    "changed_files",
    "commit_conflict_resolution",
    "commit_in_history",
    "conflict_hunks",
    "diff_context",
    "fetch",
    "head_sha",
    "in_progress_operation",
    "git_dir",
    "verify_conflict_resolution",
    "ResolutionCheck",
    "ResolutionVerification",
    "RESOLUTION_CHECKS",
    "merge_target_into_branch",
    "push_branch",
    "rebase_onto_target",
    "resolve_target_sha",
    "rev_list",
    "run_git",
    "stage_all",
]


PROTECTED_BRANCHES = frozenset({"main", "master", "trunk"})

FORBIDDEN_PUSH_FLAGS = frozenset(
    {"--force", "-f", "--force-with-lease", "--force-if-includes"}
)

# Subcommands the integration controller is allowed to reach for. Anything
# that could rewrite or discard work (``reset``, ``clean``, ``filter-branch``,
# ``update-ref``) is absent by construction.
ALLOWED_SUBCOMMANDS = frozenset(
    {
        "fetch",
        "rev-parse",
        "rev-list",
        "merge-base",
        "merge",
        "rebase",
        "push",
        "status",
        "diff",
        "add",
        "commit",
        "ls-files",
        "show",
        # Read-only: scans tracked files for leftover conflict markers.
        "grep",
        # Cleanup operations, so integration_cleanup never has to bypass this
        # guard to remove a merged worktree or delete a task branch.
        "worktree",
        "branch",
    }
)


class IntegrationGitError(RuntimeError):
    """Raised when a git operation is unsafe or fails."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class GitRunResult:
    """One completed git invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        return f"{self.stdout}{self.stderr}".strip()


@dataclass
class GitCommandLog:
    """Collects every argv a controller run executed, for the audit trail."""

    commands: list[tuple[str, ...]] = field(default_factory=list)

    def record(self, argv: Sequence[str]) -> None:
        self.commands.append(tuple(argv))

    def as_tuple(self) -> tuple[tuple[str, ...], ...]:
        return tuple(self.commands)


@dataclass(frozen=True)
class RebaseOutcome:
    ok: bool
    conflicted: bool
    conflicted_paths: tuple[str, ...]
    output: str


@dataclass(frozen=True)
class MergeOutcome:
    ok: bool
    conflicted: bool
    conflicted_paths: tuple[str, ...]
    already_up_to_date: bool
    output: str


@dataclass(frozen=True)
class PushOutcome:
    ok: bool
    argv: tuple[str, ...]
    output: str
    force_pushed: bool = False


def assert_no_force_push(argv: Sequence[str]) -> None:
    """Reject any argv that would force-push (§26 no-force-push invariant)."""
    parts = list(argv)
    forbidden = FORBIDDEN_PUSH_FLAGS & set(parts)
    if forbidden:
        raise IntegrationGitError(
            f"Force push is not allowed in V1: {', '.join(sorted(forbidden))}"
        )
    if "push" in parts:
        for part in parts[parts.index("push") + 1 :]:
            if part.startswith("+"):
                raise IntegrationGitError(
                    f"Force push refspec is not allowed in V1: {part}"
                )


def _assert_push_target_is_safe(argv: Sequence[str], *, base_branch: str | None) -> None:
    parts = list(argv)
    if "push" not in parts:
        return
    protected = set(PROTECTED_BRANCHES)
    if base_branch:
        protected.add(base_branch.strip())
    for part in parts[parts.index("push") + 1 :]:
        if part.startswith("-"):
            continue
        candidate = part.split(":")[-1]
        if candidate in protected:
            raise IntegrationGitError(
                f"Refusing to push the target/protected branch: {candidate}. "
                "Only a human merging on GitHub may advance it."
            )


def _default_runner(argv: Sequence[str], cwd: Path) -> CompletedProcessLike:
    env = dict(os.environ)
    # Never let an interactive prompt or a pager wedge an integration run.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["GIT_PAGER"] = "cat"
    # Never open an editor either. With no TTY, `git rebase --continue` fails
    # on the editor and silently leaves the rebase in progress, which review
    # blocker B2's check (a) caught. `true` accepts the prepared message as-is.
    env["GIT_EDITOR"] = "true"
    return subprocess.run(
        list(argv),
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def run_git(
    cwd: Path,
    args: Sequence[str],
    *,
    runner: Runner | None = None,
    log: GitCommandLog | None = None,
    check: bool = False,
    base_branch: str | None = None,
) -> GitRunResult:
    """Run one allowlisted git command and return its result."""
    parts = list(args)
    if not parts:
        raise IntegrationGitError("A git subcommand is required")
    subcommand = parts[0]
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise IntegrationGitError(
            f"git {subcommand} is not an allowed integration operation"
        )

    argv = ["git", *parts]
    assert_no_force_push(argv)
    _assert_push_target_is_safe(argv, base_branch=base_branch)

    if log is not None:
        log.record(argv)

    execute = runner or _default_runner
    try:
        completed = execute(argv, cwd=Path(cwd))
    except OSError as exc:  # pragma: no cover - defensive
        raise IntegrationGitError(f"git could not be executed: {exc}") from exc

    result = GitRunResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise IntegrationGitError(f"git {' '.join(parts)} failed: {result.combined}")
    return result


# -- read operations -------------------------------------------------------
def fetch(cwd: Path, *, remote: str = "origin", **kwargs) -> GitRunResult:
    """Fetch the remote so every later step sees the latest target (§44)."""
    return run_git(cwd, ["fetch", remote, "--prune"], check=True, **kwargs)


def resolve_target_sha(cwd: Path, remote: str, target_branch: str, **kwargs) -> str:
    """Resolve ``<remote>/<target>`` to a commit SHA."""
    result = run_git(cwd, ["rev-parse", f"{remote}/{target_branch}"], check=True, **kwargs)
    return result.stdout.strip()


def head_sha(cwd: Path, **kwargs) -> str:
    result = run_git(cwd, ["rev-parse", "HEAD"], check=True, **kwargs)
    return result.stdout.strip()


def behind_count(cwd: Path, branch_ref: str, target_ref: str, **kwargs) -> int:
    """Return how many target commits the branch is missing (§25.1)."""
    result = run_git(
        cwd, ["rev-list", "--count", f"{branch_ref}..{target_ref}"], check=True, **kwargs
    )
    raw = result.stdout.strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise IntegrationGitError(f"git rev-list returned a non-numeric count: {raw!r}") from exc


def rev_list(cwd: Path, ref: str, **kwargs) -> list[str]:
    result = run_git(cwd, ["rev-list", ref], check=True, **kwargs)
    return result.stdout.split()


def commit_in_history(cwd: Path, commit_sha: str, target_ref: str, **kwargs) -> bool:
    """Return True when ``commit_sha`` is an ancestor of ``target_ref`` (§36)."""
    if not commit_sha or not commit_sha.strip():
        return False
    result = run_git(
        cwd,
        ["merge-base", "--is-ancestor", commit_sha.strip(), target_ref],
        **kwargs,
    )
    return result.returncode == 0


def diff_context(cwd: Path, target_ref: str, **kwargs) -> str:
    """Return a compact diff summary against the target, for §29 evidence."""
    result = run_git(cwd, ["diff", "--stat", f"{target_ref}...HEAD"], **kwargs)
    return result.stdout.strip()


def changed_files(cwd: Path, target_ref: str, **kwargs) -> list[str]:
    result = run_git(cwd, ["diff", "--name-only", f"{target_ref}...HEAD"], **kwargs)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def conflicted_paths(cwd: Path, **kwargs) -> tuple[str, ...]:
    result = run_git(cwd, ["diff", "--name-only", "--diff-filter=U"], **kwargs)
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def conflict_hunks(cwd: Path, **kwargs) -> list[dict[str, str]]:
    """Return the conflicted files and their raw conflicted text (§27.2.1)."""
    hunks: list[dict[str, str]] = []
    for path in conflicted_paths(cwd, **kwargs):
        target = Path(cwd) / path
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        hunks.append({"path": path, "hunk": text})
    return hunks


def git_dir(cwd: Path) -> Path:
    """Return the per-worktree git directory for ``cwd``.

    A linked worktree stores a ``gitdir:`` pointer file in place of ``.git``;
    in-progress merge and rebase state lives in the directory it points to.
    """
    path = Path(cwd) / ".git"
    if path.is_file():
        pointer = path.read_text(encoding="utf-8").strip()
        prefix = "gitdir:"
        if pointer.startswith(prefix):
            target = Path(pointer[len(prefix) :].strip())
            return target if target.is_absolute() else (Path(cwd) / target).resolve()
    return path


def in_progress_operation(cwd: Path) -> str | None:
    """Return the name of an in-flight merge/rebase, or None if there is none.

    Review blocker B2, check (a), as amended by the human ruling:

    * a merge is in progress when ``MERGE_HEAD`` exists;
    * a rebase is in progress when ``rebase-merge/`` or ``rebase-apply/``
      exists in the git dir. ``REBASE_HEAD`` counts only alongside one of
      them.

    These directories are how git itself tracks an in-progress rebase, so a
    half-finished rebase cannot pass. ``REBASE_HEAD`` on its own is not
    evidence of anything: git 2.43 leaves it behind after a *successful*
    ``git rebase --continue``, and counting it flagged every correctly
    completed rebase as still running.
    """
    directory = git_dir(cwd)
    if (directory / "MERGE_HEAD").exists():
        return "merge"
    if (directory / "rebase-merge").exists() or (directory / "rebase-apply").exists():
        return "rebase"
    return None


# -- post-resolution verification (§27.2.1, review blocker B2) -------------
#
# After ANY AI conflict resolution, and before validators run, the control
# plane verifies all five checks below. The resolver's own claim that it
# "resolved" the conflict is never enough on its own.

CHECK_NO_OPERATION_IN_PROGRESS = "no_operation_in_progress"   # (a)
CHECK_WORKTREE_CLEAN = "worktree_clean"                       # (b)
CHECK_NO_CONFLICT_MARKERS = "no_conflict_markers"             # (c)
CHECK_HEAD_IS_NEW_COMMIT = "head_is_new_commit"               # (d)
CHECK_TARGET_IS_ANCESTOR = "target_is_ancestor_of_head"       # (e)

RESOLUTION_CHECKS: tuple[str, ...] = (
    CHECK_NO_OPERATION_IN_PROGRESS,
    CHECK_WORKTREE_CLEAN,
    CHECK_NO_CONFLICT_MARKERS,
    CHECK_HEAD_IS_NEW_COMMIT,
    CHECK_TARGET_IS_ANCESTOR,
)

# `<<<<<<<`, `>>>>>>>` and the diff3 `|||||||` are unambiguous in any tracked
# file. A lone `=======` is also a valid setext heading underline, so it is
# only treated as a marker in a file that actually conflicted.
_UNAMBIGUOUS_MARKER_RE = r"^(<{7}|>{7}|[|]{7})( |$)"
_SEPARATOR_MARKER_RE = r"^={7}$"

_MAX_DETAIL_LINES = 20


@dataclass(frozen=True)
class ResolutionCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ResolutionVerification:
    checks: tuple[ResolutionCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    def to_list(self) -> list[dict[str, object]]:
        return [check.to_dict() for check in self.checks]


def _first_lines(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    kept = lines[:_MAX_DETAIL_LINES]
    suffix = f"\n... {len(lines) - len(kept)} more" if len(lines) > len(kept) else ""
    return "\n".join(kept) + suffix


def _marker_scan(cwd: Path, pattern: str, paths: Sequence[str], **kwargs) -> tuple[bool, str]:
    """Return (clean, detail) for a ``git grep`` over tracked files.

    git grep exits 1 when nothing matches, 0 when something does, and >1 on
    error. An error is treated as a failure: a check that cannot run has not
    passed.
    """
    args = ["grep", "-n", "-I", "-E", "-e", pattern]
    if paths:
        args.extend(["--", *paths])
    result = run_git(cwd, args, **kwargs)
    if result.returncode == 1:
        return True, ""
    if result.returncode == 0:
        return False, _first_lines(result.stdout)
    return False, f"git grep failed: {result.combined}"


def verify_conflict_resolution(
    cwd: Path,
    *,
    head_before: str,
    target_sha: str,
    conflicted_files: Sequence[str] = (),
    **kwargs,
) -> ResolutionVerification:
    """Run the five deterministic checks after an AI conflict resolution.

    a. no rebase or merge is in progress
    b. the worktree is clean (untracked files count)
    c. no conflict markers are left in tracked files
    d. HEAD is a new commit, different from HEAD before resolution began
    e. the latest target SHA is an ancestor of HEAD
    """
    checks: list[ResolutionCheck] = []

    operation = in_progress_operation(cwd)
    checks.append(
        ResolutionCheck(
            CHECK_NO_OPERATION_IN_PROGRESS,
            operation is None,
            "" if operation is None else f"a {operation} is still in progress",
        )
    )

    status = run_git(cwd, ["status", "--porcelain=v1", "--untracked-files=all"], **kwargs)
    clean = status.ok and not status.stdout.strip()
    checks.append(
        ResolutionCheck(
            CHECK_WORKTREE_CLEAN,
            clean,
            "" if clean else (_first_lines(status.stdout) or f"git status failed: {status.combined}"),
        )
    )

    markers_clean, detail = _marker_scan(cwd, _UNAMBIGUOUS_MARKER_RE, (), **kwargs)
    if markers_clean and conflicted_files:
        markers_clean, detail = _marker_scan(
            cwd, _SEPARATOR_MARKER_RE, tuple(conflicted_files), **kwargs
        )
    checks.append(ResolutionCheck(CHECK_NO_CONFLICT_MARKERS, markers_clean, detail))

    try:
        head_after = head_sha(cwd, **kwargs)
    except IntegrationGitError as exc:
        head_after = None
        head_detail = f"HEAD could not be resolved: {exc}"
    else:
        head_detail = "" if head_after != head_before else f"HEAD is still {head_before}"
    checks.append(
        ResolutionCheck(
            CHECK_HEAD_IS_NEW_COMMIT,
            head_after is not None and head_after != head_before,
            head_detail,
        )
    )

    contained = commit_in_history(cwd, target_sha, "HEAD", **kwargs)
    checks.append(
        ResolutionCheck(
            CHECK_TARGET_IS_ANCESTOR,
            contained,
            "" if contained else f"target {target_sha} is not an ancestor of HEAD",
        )
    )

    return ResolutionVerification(tuple(checks))


# -- write operations ------------------------------------------------------
def rebase_onto_target(cwd: Path, target_ref: str, **kwargs) -> RebaseOutcome:
    """Rebase the task branch onto the latest target (§24, initial only)."""
    result = run_git(cwd, ["rebase", target_ref], **kwargs)
    if result.ok:
        return RebaseOutcome(True, False, (), result.combined)
    paths = conflicted_paths(cwd, **kwargs)
    return RebaseOutcome(False, bool(paths), paths, result.combined)


def merge_target_into_branch(cwd: Path, target_ref: str, **kwargs) -> MergeOutcome:
    """Merge the latest target into an already-published task branch (§26).

    This is the non-destructive direction: it adds a commit to the task
    branch, so the published branch is never rewritten and no force push is
    needed. It cannot advance the target branch.
    """
    result = run_git(cwd, ["merge", "--no-edit", target_ref], **kwargs)
    if result.ok:
        return MergeOutcome(
            ok=True,
            conflicted=False,
            conflicted_paths=(),
            already_up_to_date="Already up to date" in result.combined,
            output=result.combined,
        )
    paths = conflicted_paths(cwd, **kwargs)
    return MergeOutcome(False, bool(paths), paths, False, result.combined)


def abort_rebase(cwd: Path, **kwargs) -> GitRunResult:
    return run_git(cwd, ["rebase", "--abort"], **kwargs)


def abort_merge(cwd: Path, **kwargs) -> GitRunResult:
    return run_git(cwd, ["merge", "--abort"], **kwargs)


def stage_all(cwd: Path, **kwargs) -> GitRunResult:
    """Stage a conflict resolver's edits so the resolution can be committed."""
    return run_git(cwd, ["add", "-A"], check=True, **kwargs)


def commit_conflict_resolution(cwd: Path, message: str, **kwargs) -> GitRunResult:
    """Commit a resolved conflict, concluding an in-flight merge or rebase."""
    operation = in_progress_operation(cwd)
    if operation == "rebase":
        return run_git(cwd, ["rebase", "--continue"], **kwargs)
    return run_git(cwd, ["commit", "--no-edit", "-m", message], **kwargs)


def build_push_command(
    *, remote: str, branch: str, base_branch: str | None = None
) -> tuple[str, ...]:
    """Build a normal (never forced) push argv for a task branch."""
    normalized = branch.strip()
    if not normalized or normalized.startswith("-") or any(c.isspace() for c in normalized):
        raise IntegrationGitError(f"Not a simple branch name: {branch!r}")
    argv = ("git", "push", remote, normalized)
    assert_no_force_push(argv)
    _assert_push_target_is_safe(argv, base_branch=base_branch)
    return argv


def push_branch(
    cwd: Path,
    *,
    remote: str,
    branch: str,
    base_branch: str | None = None,
    **kwargs,
) -> PushOutcome:
    """Publish or update a task branch with a normal push (§26)."""
    argv = build_push_command(remote=remote, branch=branch, base_branch=base_branch)
    result = run_git(cwd, list(argv[1:]), base_branch=base_branch, **kwargs)
    if not result.ok:
        raise IntegrationGitError(f"git push failed: {result.combined}")
    return PushOutcome(ok=True, argv=argv, output=result.combined, force_pushed=False)
