#!/usr/bin/env python3
"""Audit the Level 2 Milestone 1 exit gate without mutating the database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import types

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "agent_taskflow"


def _bootstrap_source_package_without_runtime_imports() -> None:
    if "agent_taskflow" in sys.modules:
        return
    package = types.ModuleType("agent_taskflow")
    package.__file__ = str(PACKAGE_ROOT / "__init__.py")
    package.__package__ = "agent_taskflow"
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["agent_taskflow"] = package


_bootstrap_source_package_without_runtime_imports()

from agent_taskflow.m1_exit_gate import audit_m1_exit_gate  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="Exit non-zero unless every M1 gate is passed.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = audit_m1_exit_gate(
        db_path=args.db_path,
        repo_root=args.repo_root,
        evidence_dir=args.evidence_dir,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if args.require_passed and report["m1_exit_gate"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
