#!/usr/bin/env python3
"""Run the standard local validation sequence for agent-taskflow."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DEPENDENCIES = ("fastapi", "uvicorn")


@dataclass(frozen=True)
class CheckSpec:
    name: str
    command: list[str]
    required: bool = True


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: list[str]
    status: str
    return_code: int | None
    duration_seconds: float | None = None
    reason: str | None = None
    required: bool = True


def command_to_text(command: Sequence[str]) -> str:
    return " ".join(command)


def import_dependency(name: str) -> tuple[bool, str | None]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        return False, str(exc)
    return True, None


def check_required_dependencies(
    dependencies: Sequence[str] = REQUIRED_DEPENDENCIES,
) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for dependency in dependencies:
        ok, reason = import_dependency(dependency)
        if not ok:
            detail = f"{dependency}: {reason}" if reason else dependency
            missing.append(detail)
    return not missing, missing


def find_openspec() -> str | None:
    return shutil.which("openspec")


def build_required_checks(python_executable: str = sys.executable) -> list[CheckSpec]:
    return [
        CheckSpec(
            name="workflow contract validation",
            command=[
                python_executable,
                "scripts/validate_workflow_contract.py",
            ],
        ),
        CheckSpec(
            name="Mission Control golden path smoke",
            command=[
                python_executable,
                "scripts/run_mission_control_smoke.py",
                "--keep-workspace",
            ],
        ),
        CheckSpec(
            name="PiExecutor golden path smoke (fake Pi)",
            command=[
                python_executable,
                "scripts/run_pi_executor_golden_path_smoke.py",
                "--keep-workspace",
            ],
        ),
        CheckSpec(
            name="unit tests",
            command=[
                python_executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
        ),
        CheckSpec(
            name="compileall",
            command=[
                python_executable,
                "-m",
                "compileall",
                "agent_taskflow",
                "scripts",
                "tests",
            ],
        ),
    ]


def build_openspec_check(openspec_path: str | None = None) -> CheckResult | CheckSpec:
    resolved = openspec_path if openspec_path is not None else find_openspec()
    command = ["openspec", "validate", "--all", "--no-interactive"]
    if not resolved:
        return CheckResult(
            name="openspec validate",
            command=command,
            status="skipped",
            return_code=None,
            reason="openspec is not available on PATH",
            required=False,
        )
    return CheckSpec(name="openspec validate", command=command, required=False)


def should_exit_nonzero(results: Sequence[CheckResult]) -> bool:
    return any(result.required and result.status == "failed" for result in results)


def run_check(check: CheckSpec) -> CheckResult:
    print(f"\n==> {check.name}", flush=True)
    print(f"command: {command_to_text(check.command)}", flush=True)
    start = time.monotonic()
    completed = subprocess.run(
        check.command,
        cwd=REPO_ROOT,
        check=False,
    )
    duration = time.monotonic() - start
    status = "passed" if completed.returncode == 0 else "failed"
    reason = None if status == "passed" else f"command exited with {completed.returncode}"
    return CheckResult(
        name=check.name,
        command=check.command,
        status=status,
        return_code=completed.returncode,
        duration_seconds=duration,
        reason=reason,
        required=check.required,
    )


def print_environment() -> None:
    virtual_env = os.environ.get("VIRTUAL_ENV")
    print("Local validation environment", flush=True)
    print(f"sys.executable: {sys.executable}", flush=True)
    print(f"VIRTUAL_ENV: {virtual_env if virtual_env else '<not set>'}", flush=True)
    print(f"VIRTUAL_ENV set: {'yes' if virtual_env else 'no'}", flush=True)


def print_summary(results: Sequence[CheckResult]) -> None:
    print("\nLocal validation summary", flush=True)
    for result in results:
        duration = (
            f"{result.duration_seconds:.2f}s"
            if result.duration_seconds is not None
            else "-"
        )
        return_code = "-" if result.return_code is None else str(result.return_code)
        print(f"- check: {result.name}", flush=True)
        print(f"  command: {command_to_text(result.command)}", flush=True)
        print(f"  status: {result.status}", flush=True)
        print(f"  return code: {return_code}", flush=True)
        print(f"  duration: {duration}", flush=True)
        if result.reason:
            print(f"  reason: {result.reason}", flush=True)


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("run_local_validation.py does not accept arguments", file=sys.stderr)
        return 2

    results: list[CheckResult] = []
    print_environment()
    dependencies_ok, missing_dependencies = check_required_dependencies()
    if not dependencies_ok:
        reason = (
            "required Python dependencies are missing; activate the project .venv "
            "first, for example: source .venv/bin/activate. Missing: "
            + "; ".join(missing_dependencies)
        )
        results.append(
            CheckResult(
                name="Python environment dependencies",
                command=[sys.executable, "-c", "import fastapi, uvicorn"],
                status="failed",
                return_code=1,
                duration_seconds=0.0,
                reason=reason,
            )
        )
        print(f"\nERROR: {reason}", file=sys.stderr)
        print_summary(results)
        return 1

    results.append(
        CheckResult(
            name="Python environment dependencies",
            command=[sys.executable, "-c", "import fastapi, uvicorn"],
            status="passed",
            return_code=0,
            duration_seconds=0.0,
        )
    )

    checks: list[CheckSpec | CheckResult] = [
        *build_required_checks(sys.executable),
        build_openspec_check(),
    ]
    for check in checks:
        if isinstance(check, CheckResult):
            print(f"\n==> {check.name}", flush=True)
            print(f"command: {command_to_text(check.command)}", flush=True)
            print(f"skipped: {check.reason}", flush=True)
            results.append(check)
            continue
        results.append(run_check(check))

    print_summary(results)
    return 1 if should_exit_nonzero(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
