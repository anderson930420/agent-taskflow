#!/usr/bin/env python3
"""Prepare a Pi smoke task in the local task mirror.

This helper:
- Creates worktree and artifact directories.
- Writes the implementation prompt.
- Inserts TaskRecord and TaskWorktreeRecord into the mirror DB.
- Prints a JSON summary with the next dispatch command.

It does NOT:
- Call the pi binary.
- Call MiniMax or any LLM provider.
- Read, print, or require API keys.
- Modify Mission Control frontend.
- Change dispatcher runtime behavior.
- Dispatch the task.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key

DEFAULT_PROVIDER = "minimax"
DEFAULT_MODEL = "MiniMax-M2.7"
DEFAULT_PI_BIN = "pi"
DEFAULT_TOOLS = ["read", "write", "grep", "find", "ls"]
DISPATCH_SCRIPT = "scripts/run_dispatcher.py"

SMOKE_PROMPT_TEMPLATE = """This is a controlled Pi executor smoke test.

Create a file named pi_smoke_result.txt in the current working directory containing exactly:
pi-real-run-smoke-ok

Do not modify files outside the current working directory.
"""


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    """Return an absolute Path or raise ValueError."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a Pi smoke task in the local task mirror.",
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Task key for the smoke task, for example AT-PI-SMOKE.",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the agent-taskflow repository root.",
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        help="Absolute path to the directory where task artifacts will be stored.",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        help=f"LLM provider name. Default: {DEFAULT_PROVIDER}",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--pi-bin",
        default=DEFAULT_PI_BIN,
        help=f"Path to the pi binary. Default: {DEFAULT_PI_BIN}",
    )
    parser.add_argument(
        "--tools",
        default=",".join(DEFAULT_TOOLS),
        help="Comma-separated list of tools. Default: read,write,grep,find,ls",
    )
    parser.add_argument(
        "--prompt-text",
        help="Custom prompt text to write to implementation_prompt.md.",
    )
    parser.add_argument(
        "--overwrite-prompt",
        action="store_true",
        help="Overwrite implementation_prompt.md if it already exists.",
    )
    return parser


def _parse_tools(raw: str) -> list[str]:
    tools = [t.strip() for t in raw.split(",") if t.strip()]
    if not tools:
        raise ValueError("--tools must not be empty")
    return tools


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate absolute paths
    db_path = _require_absolute_path(args.db_path, "db_path")
    repo_path = _require_absolute_path(args.repo_path, "repo_path")
    artifact_root = _require_absolute_path(args.artifact_root, "artifact_root")

    # Normalize task key
    task_key = normalize_task_key(args.task_key)

    # Derived paths
    worktree_path = repo_path / ".worktrees" / task_key
    artifact_dir = artifact_root / task_key
    prompt_path = artifact_dir / "implementation_prompt.md"

    # Create directories
    worktree_path.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Write prompt if needed
    prompt_text = args.prompt_text or SMOKE_PROMPT_TEMPLATE
    write_prompt = args.overwrite_prompt or not prompt_path.exists()
    if write_prompt:
        prompt_path.write_text(prompt_text, encoding="utf-8")

    # Parse tools
    tools_list = _parse_tools(args.tools)

    # Initialize and populate the mirror DB
    store = TaskMirrorStore(db_path)
    store.init_db()

    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            hermes_task_id=f"smoke_{task_key.lower().replace('-', '_')}",
            title="Pi executor real-run smoke",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            executor="pi",
            provider=args.provider,
            model=args.model,
            tools=tools_list,
            pi_bin=args.pi_bin,
        )
    )

    store.upsert_task_worktree(
        TaskWorktreeRecord(
            task_key=task_key,
            repo_path=repo_path,
            worktree_path=worktree_path,
            branch=f"smoke/{task_key}",
            base_branch="main",
            status="active",
        )
    )

    # Build and print JSON summary
    summary = {
        "task_key": task_key,
        "db_path": str(db_path),
        "repo_path": str(repo_path),
        "worktree_path": str(worktree_path),
        "artifact_dir": str(artifact_dir),
        "prompt_path": str(prompt_path),
        "executor": "pi",
        "provider": args.provider,
        "model": args.model,
        "tools": tools_list,
        "pi_bin": args.pi_bin,
        "next_dispatch_command": (
            f"python {DISPATCH_SCRIPT} --task-key {task_key} --db-path {db_path}"
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())