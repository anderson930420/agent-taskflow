"""Read-only branch lookup in a repository's ref storage (PR #195 ruling 4b).

Ticket creation must refuse a derived branch name that already exists in the
repository. It must also run no Git command and write nothing — the SPEC §43
negative-scope tests forbid both. So this module never spawns `git`: it reads
the repository's *files* ref backend directly.

Covered:

* `.git` as a directory, or as a `gitdir:` file (linked worktrees,
  submodules), following `commondir` to where branches actually live;
* loose refs under `refs/heads/` and `refs/remotes/<remote>/`;
* `packed-refs`.

Everything here is `stat()` and `read()` only. Whatever cannot be read with
certainty fails closed with :class:`GitRefStorageError` instead of answering
"no such branch": the reftable backend, an unreadable file, or a malformed
`.git` file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


LOCAL_BRANCH_PREFIX = "refs/heads/"
REMOTE_BRANCH_PREFIX = "refs/remotes/"

_REFTABLE_CONFIG = re.compile(
    r"^\s*refstorage\s*=\s*reftable\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class GitRefStorageError(RuntimeError):
    """Raised when branch existence cannot be determined read-only."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GitRefStorageError(f"cannot read {path}: {exc}") from exc


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError as exc:
        raise GitRefStorageError(f"cannot stat {path}: {exc}") from exc


def resolve_git_common_dir(repo_path: str | Path) -> Path | None:
    """Return the directory that holds the repository's branches, or None.

    None means `repo_path` is not a Git working tree, or does not exist, so it
    has no branches to collide with.
    """
    dot_git = Path(repo_path) / ".git"
    try:
        is_dir = dot_git.is_dir()
    except OSError as exc:
        raise GitRefStorageError(f"cannot stat {dot_git}: {exc}") from exc

    if is_dir:
        git_dir = dot_git
    elif _is_file(dot_git):
        content = _read_text(dot_git).strip()
        if not content.startswith("gitdir:"):
            raise GitRefStorageError(f"malformed .git file: {dot_git}")
        target = Path(content[len("gitdir:"):].strip())
        git_dir = target if target.is_absolute() else dot_git.parent / target
        if not git_dir.is_dir():
            raise GitRefStorageError(
                f".git file points at a missing gitdir: {git_dir}"
            )
    else:
        return None

    commondir_file = git_dir / "commondir"
    if _is_file(commondir_file):
        common = Path(_read_text(commondir_file).strip())
        git_dir = common if common.is_absolute() else git_dir / common
    return Path(os.path.normpath(git_dir))


def _ensure_files_backend(common_dir: Path) -> None:
    config = common_dir / "config"
    reftable = (common_dir / "reftable").exists() or (
        _is_file(config) and _REFTABLE_CONFIG.search(_read_text(config)) is not None
    )
    if reftable:
        raise GitRefStorageError(
            f"{common_dir} uses the reftable ref backend, which cannot be read "
            "without running git; refusing rather than guessing"
        )


def _packed_refs(common_dir: Path) -> set[str]:
    path = common_dir / "packed-refs"
    if not _is_file(path):
        return set()
    refs: set[str] = set()
    for line in _read_text(path).splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        _sha, _space, ref = line.partition(" ")
        if ref:
            refs.add(ref.strip())
    return refs


def _loose_ref_exists(common_dir: Path, ref: str) -> bool:
    return _is_file(common_dir.joinpath(*ref.split("/")))


def _validate_branch(branch: str) -> str:
    parts = branch.split("/")
    if not branch or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe branch name: {branch!r}")
    return branch


def find_existing_branch_refs(repo_path: str | Path, branch: str) -> tuple[str, ...]:
    """Return every ref in `repo_path` that names `branch`, sorted.

    Local (`refs/heads/<branch>`) and remote-tracking
    (`refs/remotes/<remote>/<branch>`), loose or packed. An empty tuple means
    no such branch exists. Spawns no process and writes nothing.
    """
    _validate_branch(branch)
    common_dir = resolve_git_common_dir(repo_path)
    if common_dir is None:
        return ()
    _ensure_files_backend(common_dir)

    packed = _packed_refs(common_dir)
    found: set[str] = set()

    local = LOCAL_BRANCH_PREFIX + branch
    if local in packed or _loose_ref_exists(common_dir, local):
        found.add(local)

    for ref in packed:
        if ref.startswith(REMOTE_BRANCH_PREFIX):
            remote, _slash, rest = ref[len(REMOTE_BRANCH_PREFIX):].partition("/")
            if remote and rest == branch:
                found.add(ref)

    remotes_dir = common_dir / "refs" / "remotes"
    try:
        remotes = [entry.name for entry in remotes_dir.iterdir() if entry.is_dir()]
    except FileNotFoundError:
        remotes = []
    except OSError as exc:
        raise GitRefStorageError(f"cannot list {remotes_dir}: {exc}") from exc
    for remote in remotes:
        ref = f"{REMOTE_BRANCH_PREFIX}{remote}/{branch}"
        if _loose_ref_exists(common_dir, ref):
            found.add(ref)

    return tuple(sorted(found))


__all__ = [
    "GitRefStorageError",
    "LOCAL_BRANCH_PREFIX",
    "REMOTE_BRANCH_PREFIX",
    "find_existing_branch_refs",
    "resolve_git_common_dir",
]
