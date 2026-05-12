#!/usr/bin/env python3
"""Run the Agent Taskflow Mission Control API server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uvicorn
from agent_taskflow.api.main import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Agent Taskflow Mission Control API server.",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the server to. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8100,
        help="Port to bind the server to. Default: 8100",
    )
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=["debug", "info", "warning", "error", "critical"],
        help="Uvicorn log level. Default: warning",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    app = create_app(db_path=args.db_path)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()