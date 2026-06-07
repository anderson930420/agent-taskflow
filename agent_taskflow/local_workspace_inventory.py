"""Read-only local worktree / tmp worktree / dirty backup inventory (P2-a).

This module collects a strictly read-only inventory of the local Git worktrees
attached to an Agent Taskflow repository. It is the first phase (P2-a) of the
local workspace cleanup effort: it *inventories and recommends*, it never
deletes, prunes, resets, cleans, or otherwise mutates anything.

For each worktree reported by ``git worktree list --porcelain`` it records the
path, whether the path still exists on disk, the branch / HEAD / detached
status, whether the worktree record is missing or prunable, whether the path is
inside ``/tmp``, whether it matches the known cron runtime worktree, whether it
matches the known dirty/manual checkout, whether it has local changes (via a
read-only ``git status --short``), a capped list of changed paths, and the
presence of common local-only directories/files. It then derives a per-worktree
recommendation and human-readable reason strings.

The module is read-only by construction. It runs only ``git worktree list
--porcelain`` and ``git status --short`` (both read-only), and otherwise only
calls ``Path.exists``. It performs no ``git clean``, no ``git reset``, no ``git
worktree remove``, no ``git worktree prune``, no ``rm``, no DB write, no crontab
write, no GitHub call, and starts no executor or validator. The next phase
(P2-b) is where explicit, human-confirmed cleanup actions live; this phase does
not perform them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Protocol


LOCAL_WORKSPACE_INVENTORY_SCHEMA_VERSION = "local_workspace_inventory.v1"
LOCAL_WORKSPACE_INVENTORY_SOURCE = "local_workspace_inventory"

# Defaults the CLI mirrors. The cron runtime worktree must be preserved; the
# manual/dirty checkout must be reviewed by a human before any cleanup.
DEFAULT_REPO_ROOT = "/home/ubuntu/agent-taskflow"
DEFAULT_RUNTIME_WORKTREE = "/home/ubuntu/agent-taskflow-cron"
DEFAULT_MANUAL_REVIEW_WORKTREE = "/home/ubuntu/agent-taskflow"
DEFAULT_PATH_PREFIXES = ("/tmp", "/home/ubuntu")
DEFAULT_STATUS_LIMIT = 20

# The temp prefix used for throwaway / candidate worktrees.
TMP_PREFIX = "/tmp"

# Per-worktree recommendation codes. Keep these stable; docs and operator
# runbooks reference them directly.
RECOMMENDATION_KEEP_RUNTIME = "keep_runtime"
RECOMMENDATION_MANUAL_REVIEW_DIRTY = "manual_review_dirty_checkout"
RECOMMENDATION_CANDIDATE_TMP = "candidate_tmp_worktree_review"
RECOMMENDATION_PRUNABLE_MISSING = "prunable_missing_worktree_record"
RECOMMENDATION_CLEAN_NON_RUNTIME = "clean_non_runtime_review"
RECOMMENDATION_NO_ACTION = "no_action"

RECOMMENDATIONS: tuple[str, ...] = (
    RECOMMENDATION_KEEP_RUNTIME,
    RECOMMENDATION_MANUAL_REVIEW_DIRTY,
    RECOMMENDATION_CANDIDATE_TMP,
    RECOMMENDATION_PRUNABLE_MISSING,
    RECOMMENDATION_CLEAN_NON_RUNTIME,
    RECOMMENDATION_NO_ACTION,
)

# Common local-only markers worth surfacing before any cleanup. Trailing
# slashes are informational; the lookup strips them.
LOCAL_ONLY_MARKERS: tuple[str, ...] = (
    ".claude/",
    "artifacts/",
    "logs/",
    "scripts/local/",
    ".agent-taskflow/",
)


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class LocalWorkspaceInventoryRequest:
    """Inputs for one read-only local workspace inventory."""

    repo_root: Path = field(default_factory=lambda: Path(DEFAULT_REPO_ROOT))
    runtime_worktrees: tuple[Path, ...] = field(
        default_factory=lambda: (Path(DEFAULT_RUNTIME_WORKTREE),)
    )
    manual_review_worktrees: tuple[Path, ...] = field(
        default_factory=lambda: (Path(DEFAULT_MANUAL_REVIEW_WORKTREE),)
    )
    path_prefixes: tuple[Path, ...] = field(
        default_factory=lambda: tuple(Path(p) for p in DEFAULT_PATH_PREFIXES)
    )
    status_limit: int = DEFAULT_STATUS_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_root", Path(self.repo_root).expanduser())
        object.__setattr__(
            self,
            "runtime_worktrees",
            tuple(Path(p).expanduser() for p in self.runtime_worktrees),
        )
        object.__setattr__(
            self,
            "manual_review_worktrees",
            tuple(Path(p).expanduser() for p in self.manual_review_worktrees),
        )
        object.__setattr__(
            self,
            "path_prefixes",
            tuple(Path(p).expanduser() for p in self.path_prefixes),
        )
        if self.status_limit < 0:
            raise ValueError("status_limit must be non-negative")


def summarize_local_workspace_inventory(
    request: LocalWorkspaceInventoryRequest,
    *,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Return a read-only inventory of local Git worktrees.

    Runs ``git worktree list --porcelain`` in ``repo_root`` and, for each
    existing worktree, a read-only ``git status --short``. Tolerates worktrees
    whose path is missing and individual git failures, recording them as
    warnings. The returned payload always includes the ``safety`` block proving
    no mutation occurred.
    """

    warnings: list[str] = []
    runtime_paths = {_normalize_path(p) for p in request.runtime_worktrees}
    manual_paths = {_normalize_path(p) for p in request.manual_review_worktrees}
    prefixes = [_normalize_path(p) for p in request.path_prefixes]

    listing = _run_command(
        ["git", "worktree", "list", "--porcelain"],
        cwd=request.repo_root,
        runner=runner,
    )

    ok = True
    entries: list[dict[str, Any]] = []
    if listing is None:
        ok = False
        warnings.append(
            f"could not run 'git worktree list --porcelain' in {request.repo_root}"
        )
    elif listing.returncode != 0:
        ok = False
        warnings.append(
            "'git worktree list --porcelain' failed: "
            + (listing.stderr.strip() or f"exit code {listing.returncode}")
        )
    else:
        entries = parse_worktree_porcelain(listing.stdout)
        if not entries:
            warnings.append("no worktrees were parsed from git worktree list output")

    worktrees: list[dict[str, Any]] = []
    for entry in entries:
        worktrees.append(
            _inspect_worktree(
                entry,
                request=request,
                runtime_paths=runtime_paths,
                manual_paths=manual_paths,
                prefixes=prefixes,
                runner=runner,
            )
        )

    summary = _build_summary(worktrees)

    return {
        "ok": ok,
        "schema_version": LOCAL_WORKSPACE_INVENTORY_SCHEMA_VERSION,
        "source": LOCAL_WORKSPACE_INVENTORY_SOURCE,
        "repo_root": str(request.repo_root),
        "runtime_worktrees": [str(p) for p in request.runtime_worktrees],
        "manual_review_worktrees": [str(p) for p in request.manual_review_worktrees],
        "path_prefixes": [str(p) for p in request.path_prefixes],
        "worktrees": worktrees,
        "summary": summary,
        "warnings": warnings,
        "safety": inventory_safety_flags(),
    }


def inventory_safety_flags() -> dict[str, bool]:
    """Return the explicit read-only safety flags for this inventory tool."""

    return {
        "read_only": True,
        "db_written": False,
        "crontab_modified": False,
        "files_deleted": False,
        "worktree_removed": False,
        "worktree_pruned": False,
        "git_reset_performed": False,
        "git_clean_performed": False,
        "github_called": False,
        "executor_started": False,
        "validator_started": False,
    }


def parse_worktree_porcelain(text: str) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` output into entry dicts.

    Each entry is delimited by a blank line. Recognized attribute lines are
    ``worktree``, ``HEAD``, ``branch``, ``detached``, ``bare``, ``locked`` and
    ``prunable``. ``locked``/``prunable`` may carry a trailing reason string.
    """

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    def flush() -> None:
        if current:
            entries.append(dict(current))
            current.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if " " in line:
            key, value = line.split(" ", 1)
        else:
            key, value = line, ""
        value = value.strip()
        if key == "worktree":
            flush()
            current["worktree"] = value
        elif key == "HEAD":
            current["HEAD"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = True
            current["locked_reason"] = value or None
        elif key == "prunable":
            current["prunable"] = True
            current["prunable_reason"] = value or None

    flush()
    return entries


def _inspect_worktree(
    entry: dict[str, Any],
    *,
    request: LocalWorkspaceInventoryRequest,
    runtime_paths: set[str],
    manual_paths: set[str],
    prefixes: list[str],
    runner: Runner | None,
) -> dict[str, Any]:
    raw_path = str(entry.get("worktree") or "")
    normalized = _normalize_path(Path(raw_path)) if raw_path else ""
    path_obj = Path(raw_path) if raw_path else None

    exists = bool(path_obj and path_obj.exists())
    porcelain_prunable = bool(entry.get("prunable"))
    missing_or_prunable = porcelain_prunable or (raw_path != "" and not exists)

    branch = _normalize_branch(entry.get("branch"))
    head = entry.get("HEAD")
    detached = bool(entry.get("detached"))
    bare = bool(entry.get("bare"))
    locked = bool(entry.get("locked"))

    inside_tmp = _is_under(normalized, _normalize_path(Path(TMP_PREFIX)))
    within_path_prefix = (not prefixes) or any(
        _is_under(normalized, prefix) for prefix in prefixes
    )
    is_runtime = normalized in runtime_paths
    is_manual_review = normalized in manual_paths

    has_local_changes: bool | None = None
    changed_paths: list[str] = []
    changed_path_count = 0
    changed_paths_truncated = False
    status_error: str | None = None

    if exists and not bare:
        status = _run_command(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=path_obj,
            runner=runner,
        )
        if status is None or status.returncode != 0:
            status_error = (
                (status.stderr.strip() if status is not None else "")
                or "git status --short failed"
            )
        else:
            all_paths = [
                _status_path(line)
                for line in status.stdout.splitlines()
                if line.strip()
            ]
            changed_path_count = len(all_paths)
            has_local_changes = changed_path_count > 0
            limit = request.status_limit
            changed_paths = all_paths[:limit]
            changed_paths_truncated = changed_path_count > len(changed_paths)

    local_only_markers, present_markers = _detect_local_only_markers(path_obj, exists)

    recommendation, reasons = _recommend(
        exists=exists,
        missing_or_prunable=missing_or_prunable,
        porcelain_prunable=porcelain_prunable,
        within_path_prefix=within_path_prefix,
        is_runtime=is_runtime,
        is_manual_review=is_manual_review,
        inside_tmp=inside_tmp,
        has_local_changes=has_local_changes,
        status_error=status_error,
        prunable_reason=entry.get("prunable_reason"),
    )

    return {
        "path": raw_path,
        "exists": exists,
        "branch": branch,
        "head": head,
        "detached": detached,
        "bare": bare,
        "locked": locked,
        "locked_reason": entry.get("locked_reason"),
        "prunable": porcelain_prunable,
        "prunable_reason": entry.get("prunable_reason"),
        "missing_or_prunable": missing_or_prunable,
        "inside_tmp": inside_tmp,
        "within_path_prefix": within_path_prefix,
        "is_runtime": is_runtime,
        "is_manual_review": is_manual_review,
        "has_local_changes": has_local_changes,
        "changed_path_count": changed_path_count,
        "changed_paths": changed_paths,
        "changed_paths_truncated": changed_paths_truncated,
        "status_error": status_error,
        "local_only_markers": local_only_markers,
        "present_local_only_markers": present_markers,
        "recommendation": recommendation,
        "reasons": reasons,
    }


def _recommend(
    *,
    exists: bool,
    missing_or_prunable: bool,
    porcelain_prunable: bool,
    within_path_prefix: bool,
    is_runtime: bool,
    is_manual_review: bool,
    inside_tmp: bool,
    has_local_changes: bool | None,
    status_error: str | None,
    prunable_reason: str | None,
) -> tuple[str, list[str]]:
    """Derive a recommendation and reason strings for one worktree.

    Ordering is deliberately safety-first: missing/prunable records first, the
    cron runtime is always kept, the manual checkout and any dirty checkout are
    routed to manual review, tmp worktrees are review candidates, and only a
    clean non-runtime worktree is a plain review candidate.
    """

    reasons: list[str] = []

    if missing_or_prunable:
        if porcelain_prunable:
            detail = f": {prunable_reason}" if prunable_reason else ""
            reasons.append(f"git reports this worktree record as prunable{detail}")
        if not exists:
            reasons.append("worktree path does not exist on disk")
        reasons.append("a 'git worktree prune' candidate for a later confirmed phase")
        return RECOMMENDATION_PRUNABLE_MISSING, reasons

    if not within_path_prefix:
        reasons.append("path is outside the configured inventory path prefixes")
        reasons.append("out of inventory scope; no recommendation made")
        return RECOMMENDATION_NO_ACTION, reasons

    if is_runtime:
        reasons.append("matches the known cron runtime worktree")
        reasons.append("must be preserved; cron executes from here")
        return RECOMMENDATION_KEEP_RUNTIME, reasons

    if is_manual_review:
        reasons.append("matches the known dirty/manual checkout path")
        if has_local_changes:
            reasons.append("has local changes via read-only git status --short")
        elif has_local_changes is False:
            reasons.append("currently clean, but retained for manual review")
        reasons.append("requires human review before any cleanup")
        return RECOMMENDATION_MANUAL_REVIEW_DIRTY, reasons

    if has_local_changes:
        reasons.append("has local changes via read-only git status --short")
        reasons.append("dirty checkout requires human review before any cleanup")
        return RECOMMENDATION_MANUAL_REVIEW_DIRTY, reasons

    if inside_tmp:
        reasons.append("path is inside /tmp")
        if has_local_changes is False:
            reasons.append("no local changes detected")
        reasons.append("candidate tmp worktree to review for a later confirmed phase")
        return RECOMMENDATION_CANDIDATE_TMP, reasons

    if has_local_changes is False:
        reasons.append("clean non-runtime worktree")
        reasons.append("review before any cleanup in a later confirmed phase")
        return RECOMMENDATION_CLEAN_NON_RUNTIME, reasons

    if status_error is not None:
        reasons.append(f"local change state could not be determined: {status_error}")
    reasons.append("no automatic recommendation; manual review encouraged")
    return RECOMMENDATION_NO_ACTION, reasons


def _detect_local_only_markers(
    path_obj: Path | None,
    exists: bool,
) -> tuple[dict[str, bool], list[str]]:
    markers: dict[str, bool] = {}
    present: list[str] = []
    for marker in LOCAL_ONLY_MARKERS:
        found = False
        if exists and path_obj is not None:
            found = (path_obj / marker.rstrip("/")).exists()
        markers[marker] = found
        if found:
            present.append(marker)
    return markers, present


def _build_summary(worktrees: list[dict[str, Any]]) -> dict[str, Any]:
    recommendation_counts = {recommendation: 0 for recommendation in RECOMMENDATIONS}
    existing = 0
    missing_or_prunable = 0
    dirty = 0
    runtime = 0
    tmp = 0
    for worktree in worktrees:
        if worktree.get("exists"):
            existing += 1
        if worktree.get("missing_or_prunable"):
            missing_or_prunable += 1
        if worktree.get("has_local_changes") is True:
            dirty += 1
        if worktree.get("is_runtime"):
            runtime += 1
        if worktree.get("inside_tmp"):
            tmp += 1
        recommendation = worktree.get("recommendation")
        if recommendation in recommendation_counts:
            recommendation_counts[recommendation] += 1
        else:
            recommendation_counts[str(recommendation)] = (
                recommendation_counts.get(str(recommendation), 0) + 1
            )

    return {
        "total_worktrees": len(worktrees),
        "existing_count": existing,
        "missing_or_prunable_count": missing_or_prunable,
        "dirty_count": dirty,
        "runtime_count": runtime,
        "tmp_count": tmp,
        "recommendation_counts": recommendation_counts,
    }


def render_local_workspace_inventory_summary(summary: dict[str, Any]) -> str:
    """Render a human-readable view of the inventory summary."""

    lines: list[str] = []
    lines.append("Local Workspace Inventory (P2-a, read-only)")
    lines.append("===========================================")
    lines.append("")
    lines.append(f"Repo root: {summary.get('repo_root')}")
    lines.append(
        "Runtime worktrees: "
        + (", ".join(summary.get("runtime_worktrees") or []) or "(none)")
    )
    lines.append(
        "Manual-review worktrees: "
        + (", ".join(summary.get("manual_review_worktrees") or []) or "(none)")
    )
    lines.append("")

    worktrees = summary.get("worktrees") or []
    lines.append(f"Worktrees ({len(worktrees)}):")
    for worktree in worktrees:
        lines.append(f"  - {worktree.get('path')}")
        lines.append(
            "      branch="
            f"{worktree.get('branch') or '(detached)'} "
            f"exists={worktree.get('exists')} "
            f"inside_tmp={worktree.get('inside_tmp')} "
            f"dirty={worktree.get('has_local_changes')}"
        )
        lines.append(f"      recommendation: {worktree.get('recommendation')}")
        for reason in worktree.get("reasons") or []:
            lines.append(f"        - {reason}")
        changed_paths = worktree.get("changed_paths") or []
        if changed_paths:
            shown = ", ".join(changed_paths)
            suffix = " (truncated)" if worktree.get("changed_paths_truncated") else ""
            lines.append(
                f"      changed ({worktree.get('changed_path_count')}): {shown}{suffix}"
            )
        present_markers = worktree.get("present_local_only_markers") or []
        if present_markers:
            lines.append(f"      local-only markers: {', '.join(present_markers)}")

    lines.append("")
    stats = summary.get("summary") or {}
    lines.append("Summary:")
    lines.append(f"  total worktrees: {stats.get('total_worktrees')}")
    lines.append(f"  existing: {stats.get('existing_count')}")
    lines.append(f"  missing/prunable: {stats.get('missing_or_prunable_count')}")
    lines.append(f"  dirty: {stats.get('dirty_count')}")
    lines.append(f"  runtime: {stats.get('runtime_count')}")
    lines.append(f"  tmp: {stats.get('tmp_count')}")

    lines.append("")
    lines.append("Recommendation counts:")
    recommendation_counts = stats.get("recommendation_counts") or {}
    for recommendation in RECOMMENDATIONS:
        lines.append(
            f"  {recommendation}: {recommendation_counts.get(recommendation, 0)}"
        )

    warnings = summary.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    lines.append("")
    lines.append(
        "Safety: read-only. No delete, no git worktree remove, no git worktree "
        "prune, no git reset, no git clean, no DB write, no crontab change, no "
        "GitHub call, no executor or validator run. P2-b is where explicit, "
        "human-confirmed cleanup happens after this review."
    )
    lines.append("")
    return "\n".join(lines)


def _status_path(line: str) -> str:
    """Extract the path portion from a ``git status --short`` line."""

    if len(line) > 3 and line[2] == " ":
        return line[3:].strip()
    return line.strip()


def _normalize_branch(branch: Any) -> str | None:
    if not isinstance(branch, str) or not branch:
        return None
    prefix = "refs/heads/"
    if branch.startswith(prefix):
        return branch[len(prefix):]
    return branch


def _normalize_path(path: Path) -> str:
    text = str(path)
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _is_under(path: str, prefix: str) -> bool:
    if not path or not prefix:
        return False
    if path == prefix:
        return True
    return path.startswith(prefix.rstrip("/") + "/")


def _run_command(
    command: list[str],
    *,
    cwd: Path | None,
    runner: Runner | None,
) -> CompletedProcessLike | None:
    try:
        return (runner or _default_runner)(
            command,
            cwd=cwd,
            shell=False,
            check=False,
            text=True,
            stdout=_PIPE,
            stderr=_PIPE,
        )
    except OSError:  # pragma: no cover - defensive runtime guard
        return None


def _default_runner(*args: Any, **kwargs: Any) -> CompletedProcessLike:
    import subprocess

    return subprocess.run(*args, **kwargs)


# ``subprocess.PIPE`` is imported lazily to keep the public module surface small
# and make the runner easy to monkeypatch in tests.
_PIPE = __import__("subprocess").PIPE


def to_json(summary: dict[str, Any]) -> str:
    """Serialize an inventory summary to deterministic JSON."""

    return json.dumps(summary, indent=2, sort_keys=True)
