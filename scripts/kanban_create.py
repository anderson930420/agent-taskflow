#!/usr/bin/env python3
"""
kanban_create.py — Safe Kanban Task Submitter for Agent Taskflow

Creates verified git worktrees and Hermes Kanban tasks for any project
configured in config/projects.yaml, without relying on --workspace worktree
auto-binding.

Safety constraints:
  - Always creates worktree under .worktrees/<task-key>
  - Uses --workspace dir:<path> (never --workspace worktree)
  - Never pushes, merges, resets hard, or cleans
  - Requires main branch unless --allow-non-main is passed
  - Rejects unsafe task keys (only [A-Za-z0-9._-]+ allowed)
  - Never overwrites existing worktrees without --reuse-existing-worktree

Usage:
    python3 scripts/kanban_create.py --config config/projects.yaml \\
        --project agent-taskflow \\
        --task-key AT-0001 \\
        --title "My task title" \\
        --body-file /tmp/task.md \\
        --assignee my-profile \\
        --priority 1 \\
        --max-runtime 2h

With --project omitted, defaults to 'agent-taskflow'.
"""

import argparse
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

# Allow direct execution via:
#   python3 scripts/kanban_create.py ...
# while still importing the repo-root package.
_REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORTS))

from agent_taskflow.artifacts import artifact_dir_for as package_artifact_dir_for
from agent_taskflow.projects import get_project_config, load_projects_config
from agent_taskflow.worktree import worktree_path_from_base

DEFAULT_CONFIG_PATH = "config/projects.yaml"
DEFAULT_PROJECT = "agent-taskflow"

# ------------------------------------------------------------------
# Constants (set after config load)
# ------------------------------------------------------------------
REPO_ROOT: Optional[Path] = None
WORKTREE_BASE: Optional[Path] = None
ARTIFACTS_BASE: Optional[Path] = None
TASK_KEY_PREFIX: Optional[str] = None
BRANCH_PREFIX: Optional[str] = None
ALLOWED_KEY_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")

# Forbidden git commands (safety)
FORBIDDEN_CMDS = {"push", "merge", "reset", "clean"}

GOVERNANCE_HEADER = """
---
## Governance Requirements (MANDATORY)

This task was created by `scripts/kanban_create.py` using a verified worktree
under `dir:` workspace (not `--workspace worktree`).

**You MUST follow these rules:**
- Work ONLY in the verified worktree: `dir:{worktree_path}`
- Do NOT run `git push`
- Do NOT run `git merge`
- Do NOT run `git reset --hard`
- Do NOT run `git clean -fd`
- Do NOT self-approve your own changes
- Do NOT mark done unless the task body explicitly allows it
- Final state MUST be `blocked / waiting_for_human_review` unless the task body explicitly allows `done`
- Write all required artifacts to `~/.hermes/task-artifacts/<task-key>/`
- Record `git status --short --untracked-files=all` in artifacts

---
""".strip()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def run(*cmd: str, capture: bool = True, check: bool = True,
        stdin_input: Optional[str] = None,
        workdir: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run a command using subprocess with explicit argument list (no shell)."""
    # Safety: reject known dangerous commands passed as arguments
    for word in cmd:
        if word in FORBIDDEN_CMDS:
            raise ValueError(f"Forbidden command component: {word}")
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        input=stdin_input,
        check=check,
        cwd=workdir,
    )


def verify_repo_clean(repo_root: Path) -> None:
    """Verify the main repo is clean (no uncommitted changes)."""
    result = run("git", "status", "--porcelain", workdir=repo_root)
    if result.stdout.strip():
        print("ERROR: Main repo has uncommitted changes:", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        sys.exit(1)


def verify_on_main_branch(repo_root: Path, allow_non_main: bool) -> None:
    """Verify current branch is main (or allow override)."""
    result = run("git", "branch", "--show-current", workdir=repo_root)
    branch = result.stdout.strip()
    if branch != "main" and not allow_non_main:
        print(f"ERROR: Not on main branch (current: {branch}). "
              "Pass --allow-non-main to override.", file=sys.stderr)
        sys.exit(1)


def validate_task_key(key: str) -> None:
    """Validate task key contains only safe characters."""
    if not ALLOWED_KEY_CHARS.match(key):
        print(f"ERROR: Task key '{key}' contains unsafe characters. "
              "Allowed: [A-Za-z0-9._-]+", file=sys.stderr)
        sys.exit(1)


def worktree_path_for(key: str) -> Path:
    if WORKTREE_BASE is None:
        raise RuntimeError("WORKTREE_BASE is not configured")
    return worktree_path_from_base(WORKTREE_BASE, key)


def artifact_path_for(key: str) -> Path:
    if ARTIFACTS_BASE is None:
        raise RuntimeError("ARTIFACTS_BASE is not configured")
    return package_artifact_dir_for(key, ARTIFACTS_BASE)


def verify_worktree(wt_path: Path, branch: str) -> None:
    """Run all required verification checks on the worktree."""
    os.chdir(wt_path)

    checks = {
        "pwd": ["pwd"],
        "git rev-parse --show-toplevel": ["git", "rev-parse", "--show-toplevel"],
        "git branch --show-current": ["git", "branch", "--show-current"],
        "git worktree list": ["git", "worktree", "list"],
        "git status --short": ["git", "status", "--short", "--untracked-files=all"],
    }

    print("\n=== Worktree Verification ===")
    all_ok = True
    for name, cmd in checks.items():
        result = run(*cmd, workdir=wt_path)
        output = result.stdout.strip()
        print(f"[{name}]")
        print(output)
        # Check specific expectations
        if name == "pwd" and output != str(wt_path):
            print(f"  MISMATCH: expected {wt_path}")
            all_ok = False
        elif name == "git rev-parse --show-toplevel" and output != str(wt_path):
            print(f"  MISMATCH: expected {wt_path}")
            all_ok = False
        elif name == "git branch --show-current" and output != branch:
            print(f"  MISMATCH: expected {branch}")
            all_ok = False
    if not all_ok:
        raise RuntimeError("Worktree verification failed")


def create_artifact_folder(key: str) -> Path:
    """Create the artifact folder for the task."""
    ap = artifact_path_for(key)
    ap.mkdir(parents=True, exist_ok=True)
    return ap


# ------------------------------------------------------------------
# Main logic
# ------------------------------------------------------------------

def build_hermes_command(args: argparse.Namespace, body_with_header: str,
                          worktree_verified: Path,
                          hermes_board: str) -> list[str]:
    """Build the hermes kanban create command as a list of args."""
    cmd = [
        "hermes", "kanban", "--board", hermes_board, "create",
        args.title,
        "--assignee", args.assignee,
        "--workspace", f"dir:{worktree_verified}",
    ]
    if args.priority is not None:
        cmd.extend(["--priority", str(args.priority)])
    if args.max_runtime:
        cmd.extend(["--max-runtime", args.max_runtime])
    if args.body_file:
        # hermes kanban create supports --body BODY, not --body-file.
        # subprocess passes this as one argument, so multiline markdown is safe.
        cmd.extend(["--body", body_with_header])
    if args.json:
        cmd.append("--json")
    return cmd


def load_config(config_path: str) -> dict:
    """Load and parse config/projects.yaml."""
    return load_projects_config(config_path)


def resolve_config(config_path: str, project: str) -> dict:
    """Load project config and resolve repo_root, worktree_base, artifacts_base, task_key_prefix."""
    projects = load_config(config_path)
    p = get_project_config(projects, project)
    repo_root = Path(p["repo_path"]).resolve()
    worktree_base = Path(p["worktrees_dir"])
    artifacts_base = Path(p["artifacts_root"])
    task_key_prefix = p.get("task_key_prefix", "")
    branch_prefix = p.get("branch_prefix", "worktree/")
    # hermes_board defaults to project_slug if omitted
    hermes_board = p.get("hermes_board", p["project_slug"])
    return {
        "repo_root": repo_root,
        "worktree_base": worktree_base,
        "artifacts_base": artifacts_base,
        "task_key_prefix": task_key_prefix,
        "branch_prefix": branch_prefix,
        "hermes_board": hermes_board,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safe Kanban Task Submitter for Agent Taskflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="Path to projects config (default: config/projects.yaml)")
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Project name from config (default: {DEFAULT_PROJECT})")
    parser.add_argument("--task-key", required=True,
                        help="Task key (e.g. AT-0001). Allowed: [A-Za-z0-9._-]+")
    parser.add_argument("--title", required=True, help="Task title")
    parser.add_argument("--body-file", required=True,
                        help="Path to file containing task body (markdown)")
    parser.add_argument("--assignee", required=True, help="Assignee profile name")
    parser.add_argument("--priority", type=int, default=None)
    parser.add_argument("--max-runtime", default=None,
                        help="Max runtime (e.g. 2h, 30m)")
    parser.add_argument("--allow-non-main", action="store_true",
                        help="Allow creating from non-main branch")
    parser.add_argument("--reuse-existing-worktree", action="store_true",
                        help="Reuse an existing worktree instead of failing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands and body but do not execute")
    parser.add_argument("--json", action="store_true",
                        help="Pass --json to hermes kanban create")

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Step 0: Load project config
    # ------------------------------------------------------------------
    try:
        cfg = resolve_config(args.config, args.project)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    global REPO_ROOT, WORKTREE_BASE, ARTIFACTS_BASE, TASK_KEY_PREFIX, BRANCH_PREFIX, HERMES_BOARD
    REPO_ROOT = cfg["repo_root"]
    WORKTREE_BASE = cfg["worktree_base"]
    ARTIFACTS_BASE = cfg["artifacts_base"]
    TASK_KEY_PREFIX = cfg["task_key_prefix"]
    BRANCH_PREFIX = cfg["branch_prefix"]
    HERMES_BOARD = cfg["hermes_board"]

    print(f"Project:        {args.project}")
    print(f"Config:         {args.config}")
    print(f"Repo root:      {REPO_ROOT}")
    print(f"Worktrees dir:  {WORKTREE_BASE}")
    print(f"Artifacts dir:  {ARTIFACTS_BASE}")
    print(f"Hermes board:   {HERMES_BOARD}")

    # ------------------------------------------------------------------
    # Step 1: Validate task key
    # ------------------------------------------------------------------
    validate_task_key(args.task_key)

    # ------------------------------------------------------------------
    # Step 2: Verify we are in or against the correct repo
    # ------------------------------------------------------------------
    repo_root = REPO_ROOT.resolve()
    if not repo_root.exists():
        print(f"ERROR: Repo root does not exist: {repo_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Using repo root: {repo_root}")

    # ------------------------------------------------------------------
    # Step 3: Verify main repo is clean
    # ------------------------------------------------------------------
    print("\nChecking main repo is clean...")
    verify_repo_clean(repo_root)

    # ------------------------------------------------------------------
    # Step 4: Verify on main branch
    # ------------------------------------------------------------------
    print("Checking current branch...")
    verify_on_main_branch(repo_root, args.allow_non_main)

    # ------------------------------------------------------------------
    # Step 5: Read task body
    # ------------------------------------------------------------------
    body_path = Path(args.body_file).resolve()
    if not body_path.exists():
        print(f"ERROR: Body file does not exist: {body_path}", file=sys.stderr)
        sys.exit(1)
    original_body = body_path.read_text()

    # ------------------------------------------------------------------
    # Step 6: Prepare worktree
    # ------------------------------------------------------------------
    wt_path = worktree_path_for(args.task_key)
    branch = f"{BRANCH_PREFIX}{args.task_key}"
    wt_exists = wt_path.exists()

    if wt_exists and not args.reuse_existing_worktree:
        print(f"ERROR: Worktree already exists: {wt_path}", file=sys.stderr)
        print("Pass --reuse-existing-worktree to reuse it.", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 7: Compute artifact folder path
    # ------------------------------------------------------------------
    ap = artifact_path_for(args.task_key)
    print(f"\nArtifact folder: {ap}")

    # ------------------------------------------------------------------
    # Step 8: Build body with governance header
    # ------------------------------------------------------------------
    body_with_header = GOVERNANCE_HEADER.format(
        worktree_path=wt_path
    ) + "\n\n" + original_body

    # ------------------------------------------------------------------
    # Step 9: Dry run
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n=== DRY RUN — no changes made ===")
        print(f"Task key:   {args.task_key}")
        print(f"Title:      {args.title}")
        print(f"Assignee:   {args.assignee}")
        print(f"Worktree:   {wt_path} ({'exists' if wt_exists else 'new'})")
        print(f"Branch:     {branch}")
        print(f"Artifact:   {ap}")
        print(f"Body file:  {args.body_file}")
        if not wt_exists:
            print(f"\nWould run:")
            print(f"  git worktree add -b {branch} {wt_path} main (from {repo_root})")
        print(f"\nhermes kanban create command:")
        print(f"  hermes kanban --board {HERMES_BOARD} create ... --workspace dir:{wt_path}")
        print(f"\nBody with governance header:\n")
        print(body_with_header)
        return

    # ------------------------------------------------------------------
    # Step 10: Create artifact folder and worktree
    # ------------------------------------------------------------------
    ap = create_artifact_folder(args.task_key)

    if not wt_exists:
        print(f"\nCreating worktree: {wt_path}")
        run("git", "worktree", "add", "-b", branch, str(wt_path), "main",
            workdir=repo_root)
    else:
        print(f"\nReusing existing worktree: {wt_path}")

    # ------------------------------------------------------------------
    # Step 11: Verify worktree
    # ------------------------------------------------------------------
    print(f"\nVerifying worktree at {wt_path}...")
    verify_worktree(wt_path, branch)

    # ------------------------------------------------------------------
    # Step 12: Create Hermes Kanban task
    # ------------------------------------------------------------------
    print("\nCreating Hermes Kanban task...")
    cmd = build_hermes_command(args, body_with_header, wt_path, HERMES_BOARD)

    print(f"\nRunning: {' '.join(cmd)}")
    try:
        result = run(*cmd, check=False)
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print(f"WARNING: hermes kanban create returned {result.returncode}",
                  file=sys.stderr)
            # Don't exit — artifact folder and worktree are created successfully
    except FileNotFoundError:
        print("ERROR: 'hermes' command not found in PATH", file=sys.stderr)
        print("Task was NOT created. Worktree and artifact folder exist.",
              file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 13: Print summary
    # ------------------------------------------------------------------
    print("\n=== Summary ===")
    print(f"Task key:      {args.task_key}")
    print(f"Worktree path: {wt_path}")
    print(f"Branch:        {branch}")
    print(f"Artifact dir:  {ap}")
    print(f"Hermes task:   (check hermes kanban board for task id)")
    print("\nNext steps:")
    print(f"  1. cd {wt_path}")
    print(f"  2. Implement your changes")
    print(f"  3. Write artifacts to {ap}")
    print(f"  4. Create PR against main when ready")
    print(f"  5. After PR merges, clean up worktree:")
    print(f"       git worktree remove {wt_path}")
    print(f"       git branch -d {branch}")


if __name__ == "__main__":
    main()
