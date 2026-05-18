#!/usr/bin/env python3
"""Emit a JSON preflight report for real executor dogfood readiness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.preflight import DEFAULT_VALIDATORS, run_preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check active environment dependencies before real executor runs.",
    )
    parser.add_argument(
        "--validators",
        default=",".join(DEFAULT_VALIDATORS),
        help="Comma-separated validators to preflight. Default: pytest,openspec.",
    )
    parser.add_argument(
        "--executor",
        default="manual",
        help="Executor context to preflight, for example pi, opencode, manual, shell, or noop.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON. JSON output is the default and only output format.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when the active Python environment is not the repo .venv.",
    )
    parser.add_argument(
        "--require-openspec",
        action="store_true",
        help="Treat missing openspec as a failed required check.",
    )
    parser.add_argument(
        "--require-pytest",
        action="store_true",
        default=None,
        help="Treat missing pytest as failed. This is already true when pytest is selected.",
    )
    parser.add_argument(
        "--require-fastapi",
        action="store_true",
        help="Treat missing fastapi as a failed required check.",
    )
    parser.add_argument(
        "--require-uvicorn",
        action="store_true",
        help="Treat missing uvicorn as a failed required check.",
    )
    parser.add_argument(
        "--require-pi",
        action="store_true",
        help="Treat missing pi executable discovery as a failed required check.",
    )
    parser.add_argument(
        "--require-opencode",
        action="store_true",
        help="Treat missing opencode executable discovery as a failed required check.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_preflight(
        validators=args.validators,
        executor=args.executor,
        strict=args.strict,
        require_openspec=args.require_openspec,
        require_pytest=args.require_pytest,
        require_fastapi=args.require_fastapi,
        require_uvicorn=args.require_uvicorn,
        require_pi=args.require_pi,
        require_opencode=args.require_opencode,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
