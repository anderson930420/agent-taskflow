#!/usr/bin/env python3
"""Run the V1 Step 4 concurrency rehearsal (SPEC §19.1-§19.3) and gate it.

Everything runs against disposable databases created inside --output-dir; the
default state database is never opened. The evidence file written there is
what ``scripts/runtime_control.py set-capacity --evidence-path`` requires to
raise max_concurrent_tasks above 1.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.concurrency_gate import (  # noqa: E402
    CONCURRENCY_EVIDENCE_FILENAME,
    evaluate_concurrency_evidence,
)
from agent_taskflow.concurrency_rehearsal import run_concurrency_rehearsal  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Fresh (empty or new) directory for the disposable databases and evidence",
    )
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--lease-ttl-seconds", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    try:
        evidence = run_concurrency_rehearsal(
            output_dir=output,
            repo_root=REPO_ROOT,
            threads=args.threads,
            processes=args.processes,
            lease_ttl_seconds=args.lease_ttl_seconds,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    evidence_path = output / CONCURRENCY_EVIDENCE_FILENAME
    gate = evaluate_concurrency_evidence(evidence_path, repo_root=REPO_ROOT)
    ok = evidence["all_checks_passed"] and gate["gate"] == "passed"
    print(
        json.dumps(
            {
                "ok": ok,
                "evidence_path": str(evidence_path),
                "repo_sha": evidence["repo_sha"],
                "all_checks_passed": evidence["all_checks_passed"],
                "checks": evidence["checks"],
                "gate": gate,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
