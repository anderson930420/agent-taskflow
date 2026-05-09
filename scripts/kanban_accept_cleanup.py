#!/usr/bin/env python3
"""
kanban_accept_cleanup.py — Generic post-merge accept / cleanup helper for Hermes Kanban.

Standardizes the post-merge cleanup flow after a human reviewer has already accepted
and merged a PR. Records the human decision, verifies the merged commit, removes the
task worktree, deletes task branches when safe, and optionally adds a Hermes
Kanban acceptance comment / completes the task.

Safety properties:
- Never merges a PR
- Never approves work by itself
- Requires --confirm for any real cleanup / write / Hermes action
- Dry-run mode is completely side-effect free (no fetch/checkout/pull, no file writes,
  no worktree removal, no branch deletion, no Hermes comment/complete)
- Never runs: git merge, gh pr merge, git push directly to main
- Never deletes remote branch unless --delete-remote-branch is explicitly provided
- Fails if main repo is dirty
- Fails if decision is invalid
- Fails if merged commit is provided but not in base branch history
- Fails if decision=accepted but no merged commit exists (without --allow-missing-merged-commit)
- Worktree removal uses non-force by default; --force-remove-worktree enables force removal
- Uses subprocess with argument lists; no shell=True
- Uses git show-ref for robust local branch detection

Usage:

    # Dry run (always safe — no side effects)
    python3 scripts/kanban_accept_cleanup.py \\
        --project agent-taskflow \\
        --task-key AT-0011 \\
        --task-id t_example \\
        --decision accepted \\
        --merged-commit abc1234 \\
        --dry-run

    # Real run (requires --confirm)
    python3 scripts/kanban_accept_cleanup.py \\
        --project agent-taskflow \\
        --task-key AT-0011 \\
        --task-id t_example \\
        --decision accepted \\
        --merged-commit abc1234 \\
        --confirm

Exit codes:
    0  — success (dry-run printed plan, or real actions completed)
    1  — check failed (see error message)
    2  — internal error (missing config, bad args, etc.)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

DEFAULT_CONFIG_PATH = "config/projects.yaml"
VALID_DECISIONS = ("accepted", "rejected", "abandoned")


def run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command, return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 2, "", "command timed out"
    except Exception as e:
        return 2, "", str(e)


def load_config(config_path: str) -> dict:
    """Load and parse config/projects.yaml."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"config file not found: {config_path}")
    import yaml
    with open(config_path, "r") as f:
        data = yaml.safe_load(f)
    return data.get("projects", {})


def resolve_paths(config_path: str, project: str, task_key: str) -> dict:
    """Resolve repo, worktree, branch, artifact_dir from project registry."""
    projects = load_config(config_path)
    if project not in projects:
        raise ValueError(f"Project {project!r} not found in {config_path}")
    p = projects[project]
    repo = p["repo_path"]
    worktrees_dir = p["worktrees_dir"]
    artifacts_root = p["artifacts_root"]
    default_branch = p.get("default_branch", "main")
    branch_prefix = p.get("branch_prefix", "worktree/")

    worktree = os.path.join(worktrees_dir, task_key)
    branch = branch_prefix + task_key
    artifact_dir = os.path.join(artifacts_root, task_key)

    return {
        "repo": repo,
        "worktree": worktree,
        "branch": branch,
        "artifact_dir": artifact_dir,
        "default_branch": default_branch,
        "remote": "origin",
    }


def check_gh_available() -> bool:
    """Check if gh CLI is available."""
    rc, _, _ = run(["gh", "--version"])
    return rc == 0


def verify_pr_merged(pr_url: str | None, pr_number: int | None, repo: str) -> tuple[bool, str]:
    """
    Verify a PR is merged using gh CLI if available.
    Returns (verified, message).
    """
    if not check_gh_available():
        return False, "gh CLI not available — cannot verify PR merge"

    # Extract owner/repo from remote URL
    rc, remote_url, err = run(["git", "remote", "get-url", "origin"], cwd=repo)
    if rc != 0:
        return False, f"Could not get origin URL: {err}"

    owner_repo = ""
    remote_url = remote_url.strip()
    if "github.com" in remote_url:
        parts = remote_url.replace(".git", "").replace(":", "/").split("/")
        owner_repo = "/".join(parts[-2:])

    # Determine which identifier to use
    identifier = pr_url
    if not identifier and pr_number:
        identifier = f"{owner_repo}#{pr_number}" if owner_repo else str(pr_number)

    if not identifier:
        return False, "No pr_url or pr_number provided to verify"

    cmd = ["gh", "pr", "view", identifier, "--json", "state", "-q", ".state"]
    rc, state, err = run(cmd)
    if rc != 0:
        return False, f"Could not verify PR state: {err}"
    if "MERGED" in state.upper():
        return True, f"PR {identifier} is MERGED"
    return False, f"PR {identifier} state: {state}"


def verify_merged_commit(commit: str, repo: str, base_branch: str) -> tuple[bool, str]:
    """
    Verify a commit is in the base branch history.
    Returns (found, message).
    """
    # First check if it's the current HEAD
    rc, head, _ = run(["git", "rev-parse", base_branch], cwd=repo)
    if rc == 0 and head.startswith(commit):
        return True, f"Commit {commit} is current HEAD of {base_branch}"

    # Check if commit is in history using git merge-base --is-ancestor
    rc, _, _ = run(
        ["git", "merge-base", "--is-ancestor", commit, base_branch],
        cwd=repo,
    )
    if rc == 0:
        return True, f"Commit {commit} found in {base_branch} history"
    return False, f"Commit {commit} not found in {base_branch} history"


def write_decision_md(artifact_dir: str, data: dict) -> str:
    """Write decision.md artifact and return path."""
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, "decision.md")
    with open(path, "w") as f:
        f.write("# Task Decision\n\n")
        f.write(f"- project: {data['project']}\n")
        f.write(f"- task_key: {data['task_key']}\n")
        if data.get("task_id"):
            f.write(f"- task_id: {data['task_id']}\n")
        f.write(f"- decision: {data['decision']}\n")
        if data.get("pr_url"):
            f.write(f"- pr_url: {data['pr_url']}\n")
        if data.get("pr_number"):
            f.write(f"- pr_number: {data['pr_number']}\n")
        if data.get("merged_commit"):
            f.write(f"- merged_commit: {data['merged_commit']}\n")
        f.write(f"- decided_by: {data.get('decided_by', 'human')}\n")
        f.write(f"- recorded_by: kanban_accept_cleanup.py\n")
        f.write(f"- recorded_at: {data.get('recorded_at', '')}\n")
        f.write("\n## Notes\n\n")
        notes = data.get("notes", "")
        if notes:
            f.write(f"{notes}\n")
        else:
            if data["decision"] == "accepted":
                # Case A: accepted with PR/merged commit → "PR has been merged"
                # Case B: accepted with --allow-missing-merged-commit and no PR/commit → "No PR was required"
                pr_url = data.get("pr_url")
                pr_number = data.get("pr_number")
                merged_commit = data.get("merged_commit")
                has_pr_info = bool(pr_url or pr_number or merged_commit)
                if has_pr_info:
                    f.write("Human review accepted. PR has been merged to main.\n")
                else:
                    f.write("Human review accepted. No PR was required because this task produced no repo changes.\n")
            elif data["decision"] == "rejected":
                f.write("Human review rejected.\n")
            elif data["decision"] == "abandoned":
                f.write("Task was abandoned.\n")
    return path


def update_manifest(manifest_path: str, data: dict) -> bool:
    """
    Update artifact_manifest.json fields.
    Returns True if updated, False if skipped or failed.
    """
    if not os.path.exists(manifest_path):
        return False

    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        changed = False

        # Map decision to status
        if data["decision"] == "accepted":
            new_status = "done"
        elif data["decision"] == "rejected":
            new_status = "rejected"
        elif data["decision"] == "abandoned":
            new_status = "rejected"
        else:
            new_status = data["decision"]

        if "status" not in manifest or manifest["status"] not in ("done", "rejected"):
            manifest["status"] = new_status
            changed = True

        # Preserve recommendation if present (do not overwrite it)

        # Update PR fields if provided — update if field is missing, null, or empty
        if data.get("pr_url"):
            if "pr_url" not in manifest or not manifest["pr_url"]:
                manifest["pr_url"] = data["pr_url"]
                changed = True

        if data.get("merged_commit"):
            if "merged_commit" not in manifest or not manifest["merged_commit"]:
                manifest["merged_commit"] = data["merged_commit"]
                changed = True

        if changed:
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)
            return True
        return False

    except Exception:
        return False


def remove_worktree(worktree: str, repo: str, force: bool = False) -> tuple[int, str]:
    """
    Remove a git worktree using non-force removal by default.
    Returns (exit_code, message).

    BLOCKER FIX #3: Non-force by default. If worktree is dirty, fail clearly.
    Optional --force-remove-worktree enables force removal (gated by --confirm).
    """
    # First check if it's actually a worktree
    rc, wt_list, _ = run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    if rc != 0:
        return 1, f"Could not list worktrees in {repo}"

    # Check if our worktree is registered
    found = False
    for line in wt_list.splitlines():
        if line.startswith("worktree "):
            if os.path.normpath(line[len("worktree "):]) == os.path.normpath(worktree):
                found = True
                break

    if not found:
        # Worktree is not registered in git worktree list.
        # Do NOT blindly delete directories — fail clearly.
        if os.path.isdir(worktree):
            # Only remove if --force-remove-worktree was passed AND the path is
            # under repo/.worktrees/<task-key> (validated by caller via force arg).
            if force:
                try:
                    shutil.rmtree(worktree)
                    return 0, f"Force-removed unregistered worktree directory: {worktree}"
                except Exception as e:
                    return 1, f"Could not remove worktree directory: {e}"
            # Not registered AND not forcing — fail clearly.
            return 1, (
                f"Worktree {worktree} is not registered in git worktree list "
                f"and --force-remove-worktree was not passed. "
                f"Use --force-remove-worktree to force removal, or manually remove it."
            )
        return 1, f"Worktree {worktree} not found in git worktree list"

    # Non-force removal first (safer — fails if worktree is dirty)
    cmd = ["git", "worktree", "remove", worktree]
    rc, out, err = run(cmd, cwd=repo)
    if rc == 0:
        return 0, f"Removed worktree: {worktree}"

    # If non-force failed and force=True, retry with --force
    if force:
        cmd_force = ["git", "worktree", "remove", "--force", worktree]
        rc, out, err = run(cmd_force, cwd=repo)
        if rc == 0:
            return 0, f"Force-removed worktree: {worktree}"
        return rc, f"git worktree remove --force failed: {err}"

    # Non-force failed and force not requested — check if dirty
    if "dirty" in err.lower() or "modified" in err.lower() or "not an empty directory" in err.lower():
        return rc, (
            f"git worktree remove failed (worktree is dirty or not empty): {err}. "
            "Use --force-remove-worktree to override, or manually clean the worktree first."
        )
    return rc, f"git worktree remove failed: {err}"


def local_branch_exists(branch: str, repo: str) -> bool:
    """
    Robustly check if a local branch exists.
    BLOCKER FIX #4: Uses git show-ref --verify --quiet refs/heads/<branch>
    to avoid issues with raw unstripped git branch output.
    """
    rc, _, _ = run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
    )
    return rc == 0


def delete_local_branch(branch: str, repo: str) -> tuple[int, str]:
    """
    Delete a local branch if it exists and is safe to delete.
    Returns (exit_code, message).
    Uses -d (safe delete, fails if not fully merged). Does NOT use -D.
    """
    # Check if branch exists using robust git show-ref
    if not local_branch_exists(branch, repo):
        return 0, f"Branch {branch} does not exist — nothing to delete"

    # Check if it's the current branch
    rc, current, _ = run(["git", "branch", "--show-current"], cwd=repo)
    if current == branch:
        return 1, f"Cannot delete current branch {branch}"

    # Check if it's the default branch
    rc2, default_ref, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if default_ref == branch:
        return 1, f"Cannot delete default branch {branch}"

    # Delete the branch using safe -d (fails if not fully merged)
    rc, out, err = run(["git", "branch", "-d", branch], cwd=repo)
    if rc == 0:
        return 0, f"Deleted local branch: {branch}"
    return rc, f"Could not delete branch {branch} (not fully merged or still exists): {err}"


def delete_remote_branch(branch: str, remote: str, repo: str) -> tuple[int, str]:
    """
    Delete a remote branch. Requires --delete-remote-branch flag.
    Returns (exit_code, message).
    """
    rc, out, err = run(["git", "push", remote, "--delete", branch], cwd=repo)
    if rc == 0:
        return 0, f"Deleted remote branch: {remote}/{branch}"
    return rc, f"Could not delete remote branch {remote}/{branch}: {err}"


def hermes_comment(task_id: str, message: str) -> tuple[int, str]:
    """Add a Hermes Kanban comment. Returns (exit_code, message)."""
    import subprocess as sp
    try:
        proc = sp.run(
            ["hermes", "kanban", "comment", task_id, message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout.strip() or proc.stderr.strip()
    except Exception as e:
        return 2, str(e)


def hermes_complete(task_id: str) -> tuple[int, str]:
    """Complete a Hermes Kanban task. Returns (exit_code, message)."""
    import subprocess as sp
    try:
        proc = sp.run(
            ["hermes", "kanban", "complete", task_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout.strip() or proc.stderr.strip()
    except Exception as e:
        return 2, str(e)


def build_decision_data(args: argparse.Namespace, artifact_dir: str) -> dict:
    """Build the decision data dict for writing decision.md and updating manifest."""
    return {
        "project": args.project,
        "task_key": args.task_key,
        "task_id": args.task_id,
        "decision": args.decision,
        "pr_url": args.pr_url,
        "pr_number": args.pr_number,
        "merged_commit": args.merged_commit,
        "decided_by": "human",
        "recorded_by": "kanban_accept_cleanup.py",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifact_dir": artifact_dir,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hermes Kanban accept / cleanup helper — safe, human-review-first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Required
    parser.add_argument("--project", required=True, help="Project name from config/projects.yaml")
    parser.add_argument("--task-key", required=True, help="Task key (e.g. AT-0011)")
    parser.add_argument(
        "--decision",
        required=True,
        choices=VALID_DECISIONS,
        help="Decision: accepted, rejected, or abandoned",
    )
    # Optional
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to projects.yaml")
    parser.add_argument("--task-id", help="Hermes task ID (e.g. t_example)")
    parser.add_argument("--merged-commit", help="Merged commit SHA (full or short)")
    parser.add_argument("--pr-url", help="GitHub PR URL")
    parser.add_argument("--pr-number", type=int, help="GitHub PR number")
    parser.add_argument("--artifact-dir", help="Override artifact directory")
    parser.add_argument("--manifest", help="Override path to artifact_manifest.json")
    parser.add_argument("--worktree", help="Override worktree path")
    parser.add_argument("--branch", help="Override branch name")
    parser.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    parser.add_argument("--base", help="Base branch (default: project default_branch)")
    parser.add_argument(
        "--delete-remote-branch",
        action="store_true",
        help="Also delete the remote task branch (only if explicitly provided)",
    )
    parser.add_argument(
        "--skip-hermes-comment",
        action="store_true",
        help="Skip Hermes Kanban comment even if --task-id is provided",
    )
    parser.add_argument(
        "--skip-hermes-complete",
        action="store_true",
        help="Skip Hermes Kanban complete even if --task-id is provided and decision is accepted",
    )
    parser.add_argument(
        "--allow-missing-merged-commit",
        action="store_true",
        help="Allow decision=accepted without a merged commit (not recommended)",
    )
    parser.add_argument(
        "--force-remove-worktree",
        action="store_true",
        help="Use --force when removing the worktree (gated by --confirm; use with caution)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without making any changes",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm real action (required for real runs)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON summary",
    )
    args = parser.parse_args()

    # --- Validate decision ---
    if args.decision not in VALID_DECISIONS:
        print(f"ERROR: --decision must be one of {VALID_DECISIONS}", file=sys.stderr)
        return 2

    # --- Resolve config path ---
    if os.path.isabs(args.config):
        config_path = args.config
    else:
        script_dir = Path(__file__).parent.resolve()
        # Try git root first (worktree), then script dir
        rc, git_root, _ = run(["git", "rev-parse", "--show-toplevel"])
        if rc == 0 and os.path.exists(os.path.join(git_root, args.config)):
            config_path = os.path.join(git_root, args.config)
        else:
            config_path = str(script_dir / args.config)

    # --- Load config ---
    try:
        projects = load_config(config_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: failed to parse config: {e}", file=sys.stderr)
        return 2

    # --- Validate project ---
    if args.project not in projects:
        print(f"ERROR: project {args.project!r} not found in {config_path}", file=sys.stderr)
        print(f"  available projects: {', '.join(sorted(projects.keys()))}", file=sys.stderr)
        return 2

    proj = projects[args.project]

    # --- Resolve paths ---
    try:
        paths = resolve_paths(config_path, args.project, args.task_key)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    repo = paths["repo"]
    if not os.path.isabs(repo):
        script_dir = Path(__file__).parent.resolve()
        repo = str(script_dir / repo)

    expected_worktree = args.worktree or paths["worktree"]
    expected_branch = args.branch or paths["branch"]
    expected_remote = args.remote or "origin"
    default_branch = args.base or paths["default_branch"]
    artifact_dir = args.artifact_dir or paths["artifact_dir"]
    manifest_path = args.manifest or os.path.join(artifact_dir, "artifact_manifest.json")

    # --- Verify main repo exists and is a git repository ---
    if not os.path.isdir(repo):
        print(f"ERROR: repo directory does not exist: {repo}", file=sys.stderr)
        return 2

    rc, _, err = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo)
    if rc != 0:
        print(f"ERROR: repo is not a git repository: {repo}", file=sys.stderr)
        return 2

    # --- Verify main repo working tree is clean ---
    rc, main_status, _ = run(["git", "status", "--porcelain"], cwd=repo)
    if rc == 0 and main_status.strip():
        print(
            f"ERROR: Main repo has uncommitted/untracked changes — must be clean:\n  {main_status}",
            file=sys.stderr,
        )
        return 1

    # =================================================================
    # BLOCKER FIX #1: Dry-run is completely side-effect free.
    # No git fetch/checkout/pull, no file writes, no worktree removal,
    # no branch deletion, no Hermes comment/complete.
    # =================================================================
    if args.dry_run:
        # Collect read-only state info
        worktree_exists = os.path.isdir(expected_worktree)
        branch_exists = local_branch_exists(expected_branch, repo)

        remote_branch_exists = False
        if branch_exists:
            rc_rb, rb_out, _ = run(
                ["git", "ls-remote", "--heads", expected_remote, expected_branch],
                cwd=repo,
            )
            remote_branch_exists = rc_rb == 0 and expected_branch in rb_out

        rc, base_head, _ = run(["git", "rev-parse", default_branch], cwd=repo)
        base_head = base_head if rc == 0 else "(unknown)"

        # BLOCKER FIX #5: Validate merged commit in dry-run too (read-only check)
        if args.merged_commit:
            found, msg = verify_merged_commit(args.merged_commit, repo, default_branch)
            print(f"  [dry-run merged commit check] {msg}")
            if not found:
                print(f"ERROR: {msg}", file=sys.stderr)
                return 1
        elif args.decision == "accepted" and not args.allow_missing_merged_commit:
            print("ERROR: --merged-commit is required for decision=accepted (or use --allow-missing-merged-commit)", file=sys.stderr)
            return 1

        # Build decision data for planned actions (not executed)
        decision_data = build_decision_data(args, artifact_dir)

        # Build actions list (plan only — no execution in dry-run)
        actions: list[dict] = []

        # These are what WOULD be done in real mode (not actually done here)
        if os.path.isdir(artifact_dir) or args.confirm:
            actions.append({
                "action": "write_decision_md",
                "detail": f"{artifact_dir}/decision.md",
                "data": decision_data,
            })
        if os.path.exists(manifest_path):
            actions.append({
                "action": "update_manifest",
                "detail": manifest_path,
                "data": decision_data,
            })
        if args.decision == "accepted" and worktree_exists:
            force_str = " (with --force)" if args.force_remove_worktree else " (non-force)"
            actions.append({
                "action": "remove_worktree",
                "detail": expected_worktree + force_str,
            })
        if branch_exists:
            actions.append({
                "action": "delete_local_branch",
                "detail": expected_branch,
            })
        if args.delete_remote_branch and remote_branch_exists:
            actions.append({
                "action": "delete_remote_branch",
                "detail": f"{expected_remote}/{expected_branch}",
            })
        if args.task_id and not args.skip_hermes_comment:
            if args.decision == "accepted":
                if args.merged_commit or args.pr_url or args.pr_number:
                    comment_msg = (
                        f"Human review accepted. "
                        f"PR merged to main at {args.merged_commit}. "
                        f"Task cleanup completed by kanban_accept_cleanup.py."
                    )
                else:
                    comment_msg = (
                        f"Human review accepted. "
                        f"No PR was required; task produced no repo changes. "
                        f"Task cleanup completed by kanban_accept_cleanup.py."
                    )
            elif args.decision == "rejected":
                comment_msg = (
                    f"Human review rejected. "
                    f"Task closed by kanban_accept_cleanup.py."
                )
            else:
                comment_msg = (
                    f"Task abandoned. "
                    f"Closed by kanban_accept_cleanup.py."
                )
            actions.append({
                "action": "hermes_comment",
                "detail": f"task_id={args.task_id}",
                "message": comment_msg,
            })
        if args.task_id and not args.skip_hermes_complete and args.decision == "accepted":
            actions.append({
                "action": "hermes_complete",
                "detail": f"task_id={args.task_id}",
            })

        print("=== DRY RUN — no changes made ===")
        print(f"  project:       {args.project}")
        print(f"  task_key:     {args.task_key}")
        print(f"  decision:     {args.decision}")
        print(f"  repo:         {repo}")
        print(f"  worktree:     {expected_worktree} {'(exists)' if worktree_exists else '(not found)'}")
        print(f"  branch:       {expected_branch} {'(exists)' if branch_exists else '(not found)'}")
        print(f"  remote branch: {expected_remote}/{expected_branch} {'(exists)' if remote_branch_exists else '(not found)'}")
        print(f"  base:         {default_branch}")
        print(f"  base HEAD:    {base_head[:8] if base_head and base_head != '(unknown)' else base_head}")
        print(f"  remote:       {expected_remote}")
        print(f"  artifact_dir: {artifact_dir}")
        print(f"  manifest:     {manifest_path}")
        print(f"  task_id:      {args.task_id or '(not provided)'}")
        print(f"  merged_commit: {args.merged_commit or '(not provided)'}")
        print(f"  pr_url:       {args.pr_url or '(not provided)'}")
        print()
        print("Planned actions:")
        for i, act in enumerate(actions, 1):
            if act["action"] == "hermes_comment":
                print(f"  {i}. hermes_comment: {act['detail']}")
                print(f"      message: {act['message'][:80]}...")
            else:
                print(f"  {i}. {act['action']}: {act['detail']}")
        if not actions:
            print("  (no actions to perform)")
        print()
        print("Would NOT do (dry-run is side-effect free):")
        print("  - git fetch, git checkout, git pull")
        print("  - write decision.md")
        print("  - update artifact_manifest.json")
        print("  - remove worktree")
        print("  - delete local branch")
        print("  - delete remote branch")
        print("  - comment Hermes")
        print("  - complete Hermes task")

        if os.path.exists(os.path.join(artifact_dir, "decision.md")):
            print()
            print("NOTE: decision.md already exists in artifact dir — real run would overwrite it")

        return 0

    # =================================================================
    # BLOCKER FIX #2: Missing --confirm fails before any mutating action.
    # This is checked AFTER dry-run exits but BEFORE any git operations.
    # =================================================================
    if not args.confirm:
        print("ERROR: --confirm is required for real actions", file=sys.stderr)
        print("  Use --dry-run to see the plan without making changes.", file=sys.stderr)
        return 1

    # --- REAL RUN: fetch, checkout, pull ---

    # BLOCKER FIX #1: These are only reached in real mode with --confirm.
    # Dry-run exits above and never reaches here.

    print(f"Fetching {expected_remote}...")
    rc, fetch_out, fetch_err = run(["git", "fetch", expected_remote], cwd=repo)
    if rc != 0:
        print(f"WARNING: git fetch failed: {fetch_err}", file=sys.stderr)

    print(f"Checking out {default_branch} in main repo...")
    rc, co_out, co_err = run(["git", "checkout", default_branch], cwd=repo)
    if rc != 0:
        print(f"ERROR: git checkout {default_branch} failed: {co_err}", file=sys.stderr)
        return 1

    print(f"Pulling --ff-only {expected_remote} {default_branch}...")
    rc, pull_out, pull_err = run(
        ["git", "pull", "--ff-only", expected_remote, default_branch],
        cwd=repo,
    )
    if rc != 0:
        print(f"WARNING: git pull --ff-only failed: {pull_err}", file=sys.stderr)

    # --- Determine current base HEAD ---
    rc, base_head, _ = run(["git", "rev-parse", default_branch], cwd=repo)
    if rc != 0:
        base_head = "(unknown)"

    # --- Verify merged commit if provided ---
    if args.merged_commit:
        found, msg = verify_merged_commit(args.merged_commit, repo, default_branch)
        print(f"  {msg}")
        if not found:
            print(f"ERROR: {msg}", file=sys.stderr)
            return 1
    elif args.decision == "accepted" and not args.allow_missing_merged_commit:
        print(
            "ERROR: --merged-commit is required for decision=accepted "
            "(or use --allow-missing-merged-commit to skip this check)",
            file=sys.stderr,
        )
        return 1

    # --- Verify PR merge if PR info provided ---
    if args.pr_url or args.pr_number:
        verified, msg = verify_pr_merged(args.pr_url, args.pr_number, repo)
        print(f"  PR verification: {msg}")
        # Don't fail on PR verification issues — just warn

    # --- Check worktree and branch state ---
    worktree_exists = os.path.isdir(expected_worktree)
    branch_exists = local_branch_exists(expected_branch, repo)

    remote_branch_exists = False
    if branch_exists:
        rc_rb, rb_out, _ = run(
            ["git", "ls-remote", "--heads", expected_remote, expected_branch],
            cwd=repo,
        )
        remote_branch_exists = rc_rb == 0 and expected_branch in rb_out

    # --- Build actions list ---
    actions: list[dict] = []

    # Build decision data
    decision_data = build_decision_data(args, artifact_dir)

    # Action: write decision.md
    if os.path.isdir(artifact_dir) or args.confirm:
        actions.append({
            "action": "write_decision_md",
            "detail": f"{artifact_dir}/decision.md",
            "data": decision_data,
        })

    # Action: update manifest
    if os.path.exists(manifest_path):
        actions.append({
            "action": "update_manifest",
            "detail": manifest_path,
            "data": decision_data,
        })

    # Action: remove worktree (accepted only)
    if args.decision == "accepted" and worktree_exists:
        actions.append({
            "action": "remove_worktree",
            "detail": expected_worktree,
        })

    # Action: delete local branch
    if branch_exists:
        actions.append({
            "action": "delete_local_branch",
            "detail": expected_branch,
        })

    # Action: delete remote branch
    if args.delete_remote_branch and remote_branch_exists:
        actions.append({
            "action": "delete_remote_branch",
            "detail": f"{expected_remote}/{expected_branch}",
        })

    # Action: Hermes comment
    if args.task_id and not args.skip_hermes_comment:
        if args.decision == "accepted":
            if args.merged_commit or args.pr_url or args.pr_number:
                comment_msg = (
                    f"Human review accepted. "
                    f"PR merged to main at {args.merged_commit}. "
                    f"Task cleanup completed by kanban_accept_cleanup.py."
                )
            else:
                comment_msg = (
                    f"Human review accepted. "
                    f"No PR was required; task produced no repo changes. "
                    f"Task cleanup completed by kanban_accept_cleanup.py."
                )
        elif args.decision == "rejected":
            comment_msg = (
                f"Human review rejected. "
                f"Task closed by kanban_accept_cleanup.py."
            )
        else:  # abandoned
            comment_msg = (
                f"Task abandoned. "
                f"Closed by kanban_accept_cleanup.py."
            )
        actions.append({
            "action": "hermes_comment",
            "detail": f"task_id={args.task_id}",
            "message": comment_msg,
        })

    # Action: Hermes complete (accepted only)
    if (
        args.task_id
        and not args.skip_hermes_complete
        and args.decision == "accepted"
    ):
        actions.append({
            "action": "hermes_complete",
            "detail": f"task_id={args.task_id}",
        })

    # --- REAL RUN ---
    print(f"=== CONFIRMED RUN — performing {len(actions)} action(s) ===")
    print()

    results: list[str] = []
    fatal_error = False  # BLOCKER FIX #4: fatal errors stop later destructive/Hermes actions

    for i, act in enumerate(actions, 1):
        action = act["action"]
        detail = act["detail"]

        # Skip remaining actions if a fatal cleanup error already occurred
        if fatal_error:
            results.append(f"  {i}. SKIPPED (fatal error above): {action}: {detail}")
            continue

        if action == "write_decision_md":
            path = write_decision_md(artifact_dir, act["data"])
            results.append(f"  {i}. Written: {path}")

        elif action == "update_manifest":
            updated = update_manifest(manifest_path, act["data"])
            if updated:
                results.append(f"  {i}. Updated: {manifest_path}")
            else:
                results.append(f"  {i}. Skipped manifest update (not found or no change): {manifest_path}")

        elif action == "remove_worktree":
            # BLOCKER FIX #3: Use force only if --force-remove-worktree was passed
            code, msg = remove_worktree(
                expected_worktree,
                repo,
                force=args.force_remove_worktree,
            )
            if code == 0:
                results.append(f"  {i}. {msg}")
            else:
                results.append(f"  {i}. ERROR: {msg}")
                print(f"ERROR: {msg}", file=sys.stderr)
                fatal_error = True  # BLOCKER FIX #4: stop further destructive/Hermes actions

        elif action == "delete_local_branch":
            code, msg = delete_local_branch(expected_branch, repo)
            if code == 0:
                results.append(f"  {i}. {msg}")
            else:
                results.append(f"  {i}. ERROR: {msg}")
                print(f"ERROR: {msg}", file=sys.stderr)
                fatal_error = True  # BLOCKER FIX #4: stop further destructive/Hermes actions

        elif action == "delete_remote_branch":
            code, msg = delete_remote_branch(expected_branch, expected_remote, repo)
            if code == 0:
                results.append(f"  {i}. {msg}")
            else:
                results.append(f"  {i}. WARNING: {msg}")
                print(f"WARNING: {msg}", file=sys.stderr)
                # Remote branch deletion failure is non-fatal — continue

        elif action == "hermes_comment":
            code, msg = hermes_comment(args.task_id, act["message"])
            if code == 0:
                results.append(f"  {i}. Hermes comment added: {msg}")
            else:
                results.append(f"  {i}. Hermes comment failed: {msg}")
                print(f"WARNING: hermes_comment failed: {msg}", file=sys.stderr)
                # Hermes comment failure is non-fatal — continue

        elif action == "hermes_complete":
            code, msg = hermes_complete(args.task_id)
            if code == 0:
                results.append(f"  {i}. Hermes task completed: {msg}")
            else:
                # Check if already done — that is OK (not an error)
                if "already" in msg.lower() or "done" in msg.lower():
                    results.append(f"  {i}. Hermes task already done: {msg}")
                else:
                    results.append(f"  {i}. Hermes complete failed: {msg}")
                    print(f"WARNING: hermes_complete failed: {msg}", file=sys.stderr)
                    # Hermes complete failure is non-fatal — continue

    print()
    print("=== Summary ===")
    for r in results:
        print(r)

    # --- JSON output ---
    if args.json:
        output = {
            "project": args.project,
            "task_key": args.task_key,
            "decision": args.decision,
            "merged_commit": args.merged_commit,
            "base_head": base_head[:8] if base_head else None,
            "worktree_removed": args.decision == "accepted" and worktree_exists,
            "local_branch_deleted": branch_exists,
            "remote_branch_deleted": args.delete_remote_branch and remote_branch_exists,
            "hermes_completed": args.task_id and not args.skip_hermes_complete and args.decision == "accepted",
            "fatal_error": fatal_error,
            "results": results,
        }
        print()
        print(json.dumps(output, indent=2))

    # BLOCKER FIX #4: non-zero exit if fatal cleanup error occurred
    return 1 if fatal_error else 0


if __name__ == "__main__":
    sys.exit(main())
