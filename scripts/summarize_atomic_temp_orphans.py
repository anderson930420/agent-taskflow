#!/usr/bin/env python3
"""Report orphan atomic-write temporary files without modifying anything."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.atomic_temp_orphan_audit import (  # noqa: E402
    ATOMIC_TEMP_ORPHAN_AUDIT_SCHEMA_VERSION,
    ATOMIC_TEMP_ORPHAN_AUDIT_SOURCE,
    DEFAULT_MAX_ENTRIES,
    AtomicTempOrphanAuditRequest,
    atomic_temp_orphan_audit_safety_flags,
    render_atomic_temp_orphan_audit_summary,
    summarize_atomic_temp_orphans,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only inventory of orphan temporary files left by atomic "
            "artifact/evidence writes. No cleanup or other mutation is performed."
        )
    )
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="Directory tree to scan. Repeatable. Default: repository root.",
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=DEFAULT_MAX_ENTRIES,
        help=(
            "Maximum matching entries to include while still counting all matches. "
            f"Default: {DEFAULT_MAX_ENTRIES}."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = AtomicTempOrphanAuditRequest(
            roots=tuple(Path(root) for root in (args.root or [REPO_ROOT])),
            max_entries=args.max_entries,
        )
    except (TypeError, ValueError, OSError) as exc:
        if args.json:
            print(json.dumps(_error_payload(str(exc)), indent=2, sort_keys=True))
        else:
            print(f"Atomic temp orphan audit error: {exc}", file=sys.stderr)
        return 1

    audit = summarize_atomic_temp_orphans(request)
    if args.json:
        print(json.dumps(audit, indent=2, sort_keys=True))
    else:
        print(render_atomic_temp_orphan_audit_summary(audit), end="")
    return 0


def _error_payload(message: str) -> dict[str, object]:
    return {
        "ok": False,
        "schema_version": ATOMIC_TEMP_ORPHAN_AUDIT_SCHEMA_VERSION,
        "source": ATOMIC_TEMP_ORPHAN_AUDIT_SOURCE,
        "roots": [],
        "orphan_temp_files": [],
        "summary": None,
        "warnings": [message],
        "safety": atomic_temp_orphan_audit_safety_flags(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
