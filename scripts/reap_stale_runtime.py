#!/usr/bin/env python3
"""Expire stale runtime leases and reap their lock/PID markers, once.

Idempotent and explicit: it runs only when invoked. It is not a daemon and
installs no cron. Worktrees, branches and artifacts are never deleted.
"""

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

from agent_taskflow.models import require_absolute_path  # noqa: E402
from agent_taskflow.runtime_reaper import reap_stale_runtime  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        required=True,
        help="SQLite state DB to reap (required; there is no default)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    result = reap_stale_runtime(db_path)
    print(
        json.dumps(
            {
                **result.to_dict(),
                "daemon": False,
                "historical_worktrees_deleted": False,
                "historical_artifacts_deleted": False,
                "historical_branches_deleted": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
