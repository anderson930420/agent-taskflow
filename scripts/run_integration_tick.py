#!/usr/bin/env python3
"""Drain one repository's initial FIFO integration queue snapshot and exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.integration_tick import IntegrationTickRequest, run_integration_tick
from agent_taskflow.integration_validators import IntegrationValidatorSpec


def _validator_specs(path: Path) -> tuple[IntegrationValidatorSpec, ...]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise ValueError("validator config must be a nonempty JSON array")
    specs = []
    for value in values:
        if not isinstance(value, dict) or set(value) - {"name", "command", "timeout_seconds"}:
            raise ValueError("each validator needs name, command and optional timeout_seconds")
        name, command = value.get("name"), value.get("command")
        timeout = value.get("timeout_seconds")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("validator name must be a nonempty string")
        if (
            not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg for arg in command)
        ):
            raise ValueError("validator command must be a nonempty array of nonempty strings")
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ValueError("validator timeout_seconds must be a positive integer or null")
        specs.append(IntegrationValidatorSpec(name, tuple(command), timeout))
    return tuple(specs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--repo", required=True, help="Exact GitHub owner/name key stored in the queue")
    parser.add_argument("--repo-path", required=True, type=Path)
    parser.add_argument("--validator-config", required=True, type=Path)
    parser.add_argument("--target-branch", default="main")
    parser.add_argument("--remote", default="origin")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--confirm-integration", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="Read-only preview (the default)")
    args = parser.parse_args(argv)
    try:
        result = run_integration_tick(
            IntegrationTickRequest(
                repo=args.repo, repo_path=args.repo_path, db_path=args.db_path,
                validator_specs=_validator_specs(args.validator_config),
                target_branch=args.target_branch, remote=args.remote,
                dry_run=not args.confirm_integration,
                confirm_integration=args.confirm_integration,
            )
        )
    except Exception as exc:
        print(json.dumps({"kind": "integration_tick", "ok": False, "status": "error",
                          "reason": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
