#!/usr/bin/env python3
"""
kanban_workflow_regression.py — Read-only workflow regression audit.

Consolidates manual review checks from BJ-0024–BJ-0027 into one command that
checks repo, worktree, branch, artifact folder, manifest, lifecycle audit, and
optional PR metadata for a task.

Usage:
    python3 scripts/kanban_workflow_regression.py \
      --project bullet-journal \
      --task-key BJ-XXXX \
      --task-id <task-id> \
      --phase review

    python3 scripts/kanban_workflow_regression.py --project bullet-journal --task-key BJ-0027 \
      --task-id t_f9e50970 --phase post-cleanup

Exit codes:
    0 — zero failures
    1 — one or more failures
    2 — usage / config / runtime error
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = "config/projects.yaml"
ARTIFACT_ROOT_DEFAULT = "/home/ubuntu/.hermes/task-artifacts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command, return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 2, "", "command timed out"
    except Exception as e:
        return 2, "", str(e)


def load_yaml_simple(stream) -> dict:
    """Minimal YAML parser for projects.yaml (stdlib-only fallback)."""
    data = {}
    current_project = None
    for line in stream.readlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("projects:"):
            continue
        stripped = line.strip()
        if stripped.endswith(":") and not any(stripped.startswith(pre) for pre in (" ", "\t")):
            project_name = stripped.rstrip(":")
            if project_name:
                current_project = project_name
        elif line.startswith("  ") and ":" in line:
            key = line.strip().split(":")[0]
            rest = line.strip().split(":", 1)[1].strip()
            if rest and current_project:
                data.setdefault("projects", {}).setdefault(current_project, {})[key] = rest
        else:
            project_name = line.strip().rstrip(":")
            if project_name and not any(c in project_name for c in (" ", "\t")):
                current_project = project_name
    return data


def load_project_config(config_path: str, project: str) -> Optional[dict]:
    """Load project registry and return project config or None."""
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r") as f:
        data = load_yaml_simple(f)
    projects = data.get("projects", {})
    return projects.get(project)


def resolve_from_config(config_path: str, project: str, task_key: str) -> dict:
    """Resolve repo, worktree, branch, artifact_dir from project config."""
    config_path_abs = config_path
    if not os.path.isabs(config_path):
        # Resolve relative to cwd
        config_path_abs = os.path.join(os.getcwd(), config_path)

    proj = load_project_config(config_path_abs, project)
    if not proj:
        raise ValueError(f"Project {project!r} not found in {config_path_abs}")

    repo = proj["repo"]
    default_branch = proj["default_branch"]
    artifact_root = proj.get("artifact_root", ARTIFACT_ROOT_DEFAULT)
    branch_prefix = proj.get("branch_prefix", "worktree/")

    worktree = os.path.join(repo, ".worktrees", task_key)
    branch = branch_prefix + task_key
    artifact_dir = os.path.join(artifact_root, task_key)

    return {
        "repo": repo,
        "worktree": worktree,
        "branch": branch,
        "artifact_dir": artifact_dir,
        "config_path": config_path_abs,
    }


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------

def check_main_repo_exists(repo: str) -> list[dict]:
    """Check main repo exists."""
    checks = []
    exists = os.path.isdir(repo)
    checks.append({
        "name": "main_repo_exists",
        "status": "PASS" if exists else "FAIL",
        "message": f"Main repo {'exists' if exists else 'not found'}: {repo}",
    })
    return checks


def check_main_repo_status(repo: str) -> list[dict]:
    """Run git status in the main repo."""
    checks = []
    if not os.path.isdir(repo):
        checks.append({
            "name": "main_repo_status",
            "status": "WARN",
            "message": "Skipped git status (repo not found)",
        })
        return checks

    rc, stdout, stderr = run(["git", "status", "--short", "--untracked-files=all"], cwd=repo)
    # rc 0 = clean or dirty, rc 1 = error
    if rc == 0:
        lines = [l for l in stdout.strip().splitlines() if l.strip()]
        if lines:
            checks.append({
                "name": "main_repo_status",
                "status": "WARN",
                "message": f"Main repo has uncommitted changes ({len(lines)} entries): {stdout.strip()[:200]}",
            })
        else:
            checks.append({
                "name": "main_repo_status",
                "status": "PASS",
                "message": "Main repo is clean (no uncommitted changes)",
            })
    else:
        checks.append({
            "name": "main_repo_status",
            "status": "FAIL",
            "message": f"git status failed (exit {rc}): {stderr.strip() or 'unknown error'}",
        })
    return checks


def check_worktree_path(worktree: str, branch: str, phase: str, repo: str) -> list[dict]:
    """Check worktree path existence vs phase expectation."""
    checks = []
    exists = os.path.isdir(worktree)

    if phase == "review":
        # Worktree should exist
        checks.append({
            "name": "worktree_exists_review",
            "status": "PASS" if exists else "FAIL",
            "message": f"Worktree {'exists' if exists else 'not found'}: {worktree}",
        })
    elif phase == "post-handoff":
        # Worktree should exist
        checks.append({
            "name": "worktree_exists_post_handoff",
            "status": "PASS" if exists else "FAIL",
            "message": f"Worktree {'exists' if exists else 'not found'}: {worktree}",
        })
    elif phase == "post-cleanup":
        # Worktree should NOT exist
        checks.append({
            "name": "worktree_removed_post_cleanup",
            "status": "PASS" if not exists else "FAIL",
            "message": f"Worktree {'removed' if not exists else 'STILL PRESENT'}: {worktree}",
        })

    # Branch existence check — for post-cleanup, always check even if worktree is gone
    if phase == "post-cleanup" and repo:
        rc, out, _ = run(["git", "branch", "--list", branch], cwd=repo)
        branch_exists = rc == 0 and out.strip() != ""
        checks.append({
            "name": "branch_deleted_post_cleanup",
            "status": "PASS" if not branch_exists else "FAIL",
            "message": f"Branch {branch!r} {'deleted' if not branch_exists else 'STILL PRESENT'} in {repo}",
        })
    elif exists and repo:
        rc, out, _ = run(["git", "branch", "--list", branch], cwd=repo)
        branch_exists = rc == 0 and out.strip() != ""
        if phase in ("review", "post-handoff"):
            checks.append({
                "name": "branch_exists",
                "status": "PASS" if branch_exists else "FAIL",
                "message": f"Branch {branch!r} {'exists' if branch_exists else 'not found'} in {repo}",
            })
    elif exists and not repo:
        checks.append({
            "name": "branch_check_skipped",
            "status": "WARN",
            "message": "Skipped branch check (repo not available)",
        })

    return checks


def check_artifact_dir(artifact_dir: str) -> list[dict]:
    """Check artifact directory exists."""
    checks = []
    exists = os.path.isdir(artifact_dir)
    checks.append({
        "name": "artifact_dir_exists",
        "status": "PASS" if exists else "FAIL",
        "message": f"Artifact dir {'exists' if exists else 'not found'}: {artifact_dir}",
    })
    return checks


def check_required_artifact_files(artifact_dir: str, phase: str) -> list[dict]:
    """Check required artifact files exist."""
    checks = []
    required = ["artifact_manifest.json", "completion_report.md", "git_status.txt", "worktree_info.txt"]
    for fname in required:
        fpath = os.path.join(artifact_dir, fname)
        present = os.path.isfile(fpath)
        checks.append({
            "name": f"artifact_file_{fname}",
            "status": "PASS" if present else "FAIL",
            "message": f"{fname} {'present' if present else 'MISSING'} in {artifact_dir}",
        })

    # decision.md only for post-cleanup
    if phase == "post-cleanup":
        dpath = os.path.join(artifact_dir, "decision.md")
        present = os.path.isfile(dpath)
        checks.append({
            "name": "decision_md_post_cleanup",
            "status": "PASS" if present else "FAIL",
            "message": f"decision.md {'present' if present else 'MISSING'} (post-cleanup phase)",
        })
    return checks


def check_manifest_validation(manifest_path: str) -> list[dict]:
    """Validate manifest by invoking kanban_artifact_manifest.py."""
    checks = []
    if not os.path.isfile(manifest_path):
        checks.append({
            "name": "manifest_validation",
            "status": "FAIL",
            "message": f"Manifest not found: {manifest_path}",
        })
        return checks

    rc, stdout, stderr = run(
        ["python3", "scripts/kanban_artifact_manifest.py", "validate", "--path", manifest_path],
        timeout=15,
    )
    if rc == 0:
        checks.append({
            "name": "manifest_validation",
            "status": "PASS",
            "message": f"Manifest valid: {manifest_path}",
        })
    else:
        checks.append({
            "name": "manifest_validation",
            "status": "FAIL",
            "message": f"Manifest validation failed (exit {rc}): {stderr.strip() or stdout.strip()}",
        })
    return checks


def check_lifecycle_audit(project: str, task_key: str, task_id: str, artifact_dir: str, phase: str, config_path: str) -> list[dict]:
    """Run kanban_task_audit.py as a sub-check."""
    checks = []
    rc, stdout, stderr = run(
        [
            "python3", "scripts/kanban_task_audit.py",
            "--project", project,
            "--task-key", task_key,
            "--task-id", task_id,
            "--artifact-dir", artifact_dir,
            "--phase", phase,
        ],
        timeout=30,
    )
    if rc == 0:
        checks.append({
            "name": "lifecycle_audit",
            "status": "PASS",
            "message": "Lifecycle audit passed (kanban_task_audit.py exit 0)",
        })
    else:
        # Extract first line of output for context
        lines = (stdout + stderr).strip().splitlines()
        snippet = lines[0][:200] if lines else f"exit {rc}"
        checks.append({
            "name": "lifecycle_audit",
            "status": "FAIL",
            "message": f"Lifecycle audit failed (exit {rc}): {snippet}",
        })
    return checks


def load_manifest(artifact_dir: str) -> Optional[dict]:
    """Load artifact_manifest.json if present."""
    path = os.path.join(artifact_dir, "artifact_manifest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def check_manifest_consistency(manifest: Optional[dict], task_key: str, task_id: Optional[str]) -> list[dict]:
    """Check manifest field consistency."""
    checks = []
    if manifest is None:
        checks.append({
            "name": "manifest_consistency",
            "status": "FAIL",
            "message": "No manifest loaded — cannot check consistency",
        })
        return checks

    errors = []

    if manifest.get("task_key") != task_key:
        errors.append(f"task_key mismatch: manifest={manifest.get('task_key')!r}, expected={task_key!r}")
    else:
        checks.append({
            "name": "manifest_task_key_match",
            "status": "PASS",
            "message": f"task_key matches: {task_key}",
        })

    if task_id and manifest.get("task_id") != task_id:
        errors.append(f"task_id mismatch: manifest={manifest.get('task_id')!r}, expected={task_id!r}")
    elif task_id:
        checks.append({
            "name": "manifest_task_id_match",
            "status": "PASS",
            "message": f"task_id matches: {task_id}",
        })

    requires_pr = manifest.get("requires_pr")
    if not isinstance(requires_pr, bool):
        errors.append(f"requires_pr must be bool, got: {type(requires_pr)}")
    else:
        checks.append({
            "name": "manifest_requires_pr_bool",
            "status": "PASS",
            "message": f"requires_pr is bool: {requires_pr}",
        })

    changed_files = manifest.get("changed_files")
    if not isinstance(changed_files, list):
        errors.append(f"changed_files must be list, got: {type(changed_files)}")
    else:
        checks.append({
            "name": "manifest_changed_files_list",
            "status": "PASS",
            "message": f"changed_files is list ({len(changed_files)} entries)",
        })

        # if requires_pr=false, changed_files must be empty
        if requires_pr is False and len(changed_files) > 0:
            errors.append(f"requires_pr=false but changed_files is non-empty ({len(changed_files)}): {changed_files[:3]}")
        # if requires_pr=true, changed_files should be non-empty (warning, not error, if not finished yet)
        if requires_pr is True and len(changed_files) == 0:
            checks.append({
                "name": "manifest_changed_files_empty",
                "status": "WARN",
                "message": "requires_pr=true but changed_files is empty (task may not be finished yet)",
            })

    if errors:
        checks.append({
            "name": "manifest_consistency",
            "status": "FAIL",
            "message": "; ".join(errors),
        })

    return checks


def check_pr_metadata(artifact_dir: str, expect_pr: str, manifest: Optional[dict], phase: str) -> list[dict]:
    """Check PR metadata consistency.

    expect_pr: "true", "false", or "auto"

    Phase behaviour:
    - review: pr_info.json NOT required (PR handoff is future); missing is PASS/WARN only
    - post-handoff: pr_info.json required if requires_pr=true or expect-pr=true
    - post-cleanup: pr_info.json required for PR-backed tasks; not required for no-PR tasks
    """
    checks = []
    pr_info_path = os.path.join(artifact_dir, "pr_info.json")
    has_pr_info = os.path.isfile(pr_info_path)

    requires_pr = manifest.get("requires_pr") if manifest else None
    pr_url_in_manifest = manifest.get("pr_url") if manifest else None

    # Determine if PR is expected
    pr_expected = False
    if expect_pr == "true":
        pr_expected = True
    elif expect_pr == "auto":
        pr_expected = bool(requires_pr)
    elif expect_pr == "false":
        pr_expected = False

    # Load pr_info.json URL if present (for later checks)
    pr_url = None
    if has_pr_info:
        try:
            with open(pr_info_path, "r") as f:
                pr_info_data = json.load(f)
            # Support pr_url, url (fallback), and manifest pr_url
            pr_url = pr_info_data.get("pr_url") or pr_info_data.get("url") or pr_url_in_manifest
        except json.JSONDecodeError:
            pr_url = pr_url_in_manifest
    else:
        pr_url = pr_url_in_manifest

    if phase == "review":
        # During review, PR hasn't been created yet — pr_info.json is NOT required.
        # requires_pr=true means PR handoff is needed later; missing pr_info is expected.
        if has_pr_info:
            checks.append({
                "name": "pr_info_present_review",
                "status": "PASS",
                "message": f"pr_info.json present (review phase — optional at this stage): {pr_url or '(none)'}",
            })
        else:
            checks.append({
                "name": "pr_info_missing_review",
                "status": "WARN",
                "message": "pr_info.json missing (expected before PR handoff — not a review failure)",
            })
        return checks

    if phase == "post-handoff":
        # After PR creation, pr_info.json should exist if PR was expected
        if pr_expected and not has_pr_info:
            checks.append({
                "name": "pr_info_present",
                "status": "FAIL",
                "message": f"pr_info.json missing but PR expected (requires_pr={requires_pr})",
            })
        elif has_pr_info:
            checks.append({
                "name": "pr_info_present",
                "status": "PASS",
                "message": f"pr_info.json present with URL: {pr_url or '(none)'}",
            })
        else:
            checks.append({
                "name": "pr_info_not_required",
                "status": "PASS",
                "message": f"pr_info.json not required (expect-pr={expect_pr}, requires_pr={requires_pr})",
            })
        return checks

    if phase == "post-cleanup":
        # After cleanup, pr_info.json is required for PR-backed tasks only
        if requires_pr is True and not has_pr_info:
            checks.append({
                "name": "pr_info_present",
                "status": "FAIL",
                "message": "pr_info.json missing but task was PR-backed (requires_pr=true)",
            })
        elif requires_pr is True and has_pr_info:
            checks.append({
                "name": "pr_info_present",
                "status": "PASS",
                "message": f"pr_info.json present for PR-backed task: {pr_url or '(none)'}",
            })
        else:
            # requires_pr is False or null — no PR info needed
            checks.append({
                "name": "pr_info_not_required",
                "status": "PASS",
                "message": f"pr_info.json not required (requires_pr={requires_pr})",
            })
        return checks

    return checks


def check_dirty_worktree_mismatch(worktree: str, manifest: Optional[dict], repo: str) -> list[dict]:
    """Check for dirty/no-PR mismatch: if worktree exists and requires_pr=false, worktree must be clean.

    Also: if worktree exists and repo changes exist, requires_pr should be true.
    """
    checks = []
    if manifest is None:
        return checks

    requires_pr = manifest.get("requires_pr")
    worktree_exists = os.path.isdir(worktree)

    if not worktree_exists:
        return checks

    if requires_pr is False:
        # Worktree should be clean
        rc, stdout, _ = run(["git", "status", "--short", "--untracked-files=all"], cwd=worktree)
        lines = [l for l in stdout.strip().splitlines() if l.strip()]
        if lines:
            checks.append({
                "name": "worktree_dirty_requires_pr_false",
                "status": "FAIL",
                "message": f"Worktree dirty but requires_pr=false: {len(lines)} changes: {stdout.strip()[:200]}",
            })
        else:
            checks.append({
                "name": "worktree_clean_requires_pr_false",
                "status": "PASS",
                "message": "Worktree is clean and requires_pr=false",
            })

    # Check for dirty main repo with requires_pr=false mismatch
    rc, stdout, _ = run(["git", "status", "--short", "--untracked-files=all"], cwd=repo)
    main_lines = [l for l in stdout.strip().splitlines() if l.strip()]
    if main_lines and requires_pr is False:
        checks.append({
            "name": "repo_changes_requires_pr_false_mismatch",
            "status": "FAIL",
            "message": f"Repo has {len(main_lines)} uncommitted changes but requires_pr=false; may indicate requires_pr should be true",
        })
    elif main_lines and requires_pr is True:
        checks.append({
            "name": "repo_changes_requires_pr_true",
            "status": "PASS",
            "message": f"Repo has {len(main_lines)} uncommitted changes; consistent with requires_pr=true",
        })

    return checks


def check_post_cleanup_status(manifest: Optional[dict]) -> list[dict]:
    """Post-cleanup: manifest status should be one of done/accepted/rejected/abandoned."""
    checks = []
    if manifest is None:
        checks.append({
            "name": "post_cleanup_status",
            "status": "WARN",
            "message": "No manifest loaded; cannot check post-cleanup status",
        })
        return checks

    valid_statuses = {"done", "accepted", "rejected", "abandoned"}
    status = manifest.get("status")
    if status in valid_statuses:
        checks.append({
            "name": "post_cleanup_status",
            "status": "PASS",
            "message": f"Manifest status is {status!r} (valid post-cleanup status)",
        })
    else:
        checks.append({
            "name": "post_cleanup_status",
            "status": "WARN",
            "message": f"Manifest status is {status!r}; expected one of {sorted(valid_statuses)} for post-cleanup",
        })
    return checks


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_human(project: str, task_key: str, task_id: str, phase: str, checks: list[dict]) -> str:
    lines = [
        f"Workflow regression audit: {project} / {task_key}",
        f"Phase: {phase}",
    ]
    if task_id:
        lines.append(f"Task ID: {task_id}")

    passed = sum(1 for c in checks if c["status"] == "PASS")
    warnings = sum(1 for c in checks if c["status"] == "WARN")
    failures = sum(1 for c in checks if c["status"] == "FAIL")

    if failures > 0:
        summary = f"FAIL ({passed} passed, {warnings} warnings, {failures} failures)"
    elif warnings > 0:
        summary = f"WARN ({passed} passed, {warnings} warnings, {failures} failures)"
    else:
        summary = f"PASS ({passed} passed, {warnings} warnings, {failures} failures)"

    lines.append(f"Summary: {summary}")
    lines.append("")

    for c in checks:
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[c["status"]]
        lines.append(f"[{icon}] {c['name']}: {c['message']}")

    return "\n".join(lines)


def format_json(project: str, task_key: str, task_id: str, phase: str, checks: list[dict]) -> str:
    passed = sum(1 for c in checks if c["status"] == "PASS")
    warnings = sum(1 for c in checks if c["status"] == "WARN")
    failures = sum(1 for c in checks if c["status"] == "FAIL")

    return json.dumps({
        "project": project,
        "task_key": task_key,
        "task_id": task_id,
        "phase": phase,
        "summary": {
            "passed": passed,
            "warnings": warnings,
            "failures": failures,
        },
        "checks": checks,
    }, indent=2)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

SUPPORTED_PHASES = {"review", "post-handoff", "post-cleanup"}

def run_audit(project: str, task_key: str, task_id: Optional[str], phase: str,
              repo: Optional[str], artifact_dir: Optional[str], worktree: Optional[str],
              expect_pr: str, json_output: bool, config_path: str) -> int:
    """Run all checks and return exit code."""

    checks = []

    # Resolve paths from config if not provided
    try:
        resolved = resolve_from_config(config_path, project, task_key)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    repo = repo or resolved["repo"]
    worktree = worktree or resolved["worktree"]
    branch = resolved["branch"]
    artifact_dir = artifact_dir or resolved["artifact_dir"]

    # --- Repo checks ---
    checks.extend(check_main_repo_exists(repo))
    checks.extend(check_main_repo_status(repo))

    # --- Worktree checks ---
    checks.extend(check_worktree_path(worktree, branch, phase, repo))

    # --- Artifact dir checks ---
    checks.extend(check_artifact_dir(artifact_dir))

    # Load manifest for subsequent checks
    manifest = load_manifest(artifact_dir)
    manifest_path = os.path.join(artifact_dir, "artifact_manifest.json")

    # --- Required artifact files ---
    checks.extend(check_required_artifact_files(artifact_dir, phase))

    # --- Manifest validation (via script invocation) ---
    checks.extend(check_manifest_validation(manifest_path))

    # --- Manifest consistency ---
    checks.extend(check_manifest_consistency(manifest, task_key, task_id))

    # --- PR metadata ---
    checks.extend(check_pr_metadata(artifact_dir, expect_pr, manifest, phase))

    # --- Dirty worktree mismatch ---
    if manifest is not None and manifest.get("requires_pr") is not None:
        checks.extend(check_dirty_worktree_mismatch(worktree, manifest, repo))

    # --- Lifecycle audit (via script invocation) ---
    if task_id:
        checks.extend(check_lifecycle_audit(
            project, task_key, task_id, artifact_dir, phase, config_path
        ))

    # --- Post-cleanup status check ---
    if phase == "post-cleanup":
        checks.extend(check_post_cleanup_status(manifest))

    # --- Output ---
    if json_output:
        print(format_json(project, task_key, task_id or "", phase, checks))
    else:
        print(format_human(project, task_key, task_id or "", phase, checks))

    failures = sum(1 for c in checks if c["status"] == "FAIL")
    return 1 if failures > 0 else 0


def main():
    parser = argparse.ArgumentParser(
        description="Hermes Workflow Regression Audit — read-only consolidation of manual review checks",
        prog="kanban_workflow_regression.py",
    )
    parser.add_argument("--project", required=True, help="Project name (e.g. bullet-journal)")
    parser.add_argument("--task-key", required=True, help="Task key (e.g. BJ-0027)")
    parser.add_argument("--task-id", default=None, help="Hermes task ID")
    parser.add_argument("--phase", default="review", choices=list(SUPPORTED_PHASES),
                        help=f"Phase to audit (default: review). Supported: {', '.join(sorted(SUPPORTED_PHASES))}")
    parser.add_argument("--repo", default=None, help="Path to main repo (resolved from --project if not given)")
    parser.add_argument("--artifact-dir", default=None, help="Path to artifact dir (resolved from --project if not given)")
    parser.add_argument("--worktree", default=None, help="Path to worktree (resolved from --project if not given)")
    parser.add_argument("--expect-pr", default="auto", choices=["true", "false", "auto"],
                        help="Whether to expect PR metadata. 'auto' infers from manifest. (default: auto)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="Path to projects.yaml (default: config/projects.yaml)")

    args = parser.parse_args()

    exit_code = run_audit(
        project=args.project,
        task_key=args.task_key,
        task_id=args.task_id,
        phase=args.phase,
        repo=args.repo,
        artifact_dir=args.artifact_dir,
        worktree=args.worktree,
        expect_pr=args.expect_pr,
        json_output=args.json,
        config_path=args.config,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    sys.exit(main() or 0)
