#!/usr/bin/env python3
"""Run tests affected by changes since the merge base with a target ref."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_taskflow.affected_tests import changed_paths, format_selection, select_tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.repo_root.resolve()
    try:
        selection = select_tests(root, changed_paths(root, target_ref=args.base))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not select affected tests: {exc}", file=sys.stderr)
        return 1
    print(format_selection(selection), end="", flush=True)
    if not selection:
        return 0
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if importlib.util.find_spec("xdist") is not None:
        command += ["-n", "4"]
    try:
        result = subprocess.run(command + list(selection), cwd=root, check=False)
    except OSError as exc:
        print(f"Could not run pytest: {exc}", file=sys.stderr)
        return 1
    return 0 if result.returncode in (0, 5) else 1


if __name__ == "__main__":
    raise SystemExit(main())
