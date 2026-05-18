"""Read-only preflight checks for real executor dogfood runs."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


DEFAULT_VALIDATORS = ("pytest", "openspec")
RECOMMENDED_COMMANDS = (
    "python3 scripts/run_local_validation.py",
    "python -m unittest discover -s tests -v",
)
KNOWN_PI_PATHS = (Path("/home/ubuntu/tools/pi-agent/bin/pi"),)


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    kind: str
    required: bool
    status: str
    summary: str
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "kind": self.kind,
            "required": self.required,
            "status": self.status,
            "summary": self.summary,
        }
        if self.detail:
            data["detail"] = self.detail
        return data


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    status: str
    strict: bool
    executor: str
    validators: tuple[str, ...]
    python: dict[str, object]
    checks: tuple[PreflightCheck, ...]
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]
    recommended_commands: tuple[str, ...] = RECOMMENDED_COMMANDS

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "strict": self.strict,
            "executor": self.executor,
            "validators": list(self.validators),
            "python": self.python,
            "checks": [check.to_dict() for check in self.checks],
            "missing_required": list(self.missing_required),
            "missing_optional": list(self.missing_optional),
            "recommended_commands": list(self.recommended_commands),
        }


@dataclass(frozen=True)
class PreflightReport:
    result: PreflightResult

    def to_dict(self) -> dict[str, object]:
        return self.result.to_dict()


def normalize_validators(validators: str | Sequence[str] | None) -> tuple[str, ...]:
    if validators is None:
        return DEFAULT_VALIDATORS
    if isinstance(validators, str):
        raw_values = validators.split(",")
    else:
        raw_values = list(validators)
    normalized = tuple(value.strip().lower() for value in raw_values if value.strip())
    return normalized or DEFAULT_VALIDATORS


def build_python_environment(
    *,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    python_executable: str | None = None,
    python_version: str | None = None,
) -> dict[str, object]:
    resolved_repo = repo_root if repo_root is not None else Path(__file__).resolve().parents[1]
    env = environ if environ is not None else os.environ
    virtual_env = env.get("VIRTUAL_ENV")
    repo_venv = resolved_repo / ".venv"
    using_repo_venv = bool(virtual_env) and Path(virtual_env).resolve() == repo_venv.resolve()
    active_venv_exists = bool(virtual_env) and Path(virtual_env).exists()
    return {
        "executable": python_executable if python_executable is not None else sys.executable,
        "version": python_version if python_version is not None else sys.version,
        "virtual_env": virtual_env,
        "repo_venv": str(repo_venv),
        "using_repo_venv": using_repo_venv,
        "active_venv_exists": active_venv_exists,
    }


def check_python_environment(
    python_environment: Mapping[str, object],
    *,
    strict: bool,
) -> PreflightCheck:
    virtual_env = python_environment.get("virtual_env")
    using_repo_venv = bool(python_environment.get("using_repo_venv"))
    active_venv_exists = bool(python_environment.get("active_venv_exists"))
    repo_venv = str(python_environment.get("repo_venv") or "")
    if using_repo_venv:
        return PreflightCheck(
            name="python_environment",
            kind="python_runtime",
            required=True,
            status="passed",
            summary="Active Python environment is the repo .venv.",
        )
    if strict:
        if not virtual_env:
            summary = "VIRTUAL_ENV is not set; strict mode requires the repo .venv."
        elif not active_venv_exists:
            summary = "VIRTUAL_ENV does not point to an existing directory."
        else:
            summary = f"Active virtual environment is not repo .venv: {repo_venv}."
        return PreflightCheck(
            name="python_environment",
            kind="python_runtime",
            required=True,
            status="failed",
            summary=summary,
        )
    if not virtual_env:
        summary = "VIRTUAL_ENV is not set; activate .venv before real executor runs."
    elif not active_venv_exists:
        summary = "VIRTUAL_ENV does not point to an existing directory."
    else:
        summary = f"Active virtual environment is not repo .venv: {repo_venv}."
    return PreflightCheck(
        name="python_environment",
        kind="python_runtime",
        required=False,
        status="warning",
        summary=summary,
    )


def check_python_import(
    name: str,
    *,
    required: bool,
    selected: bool = True,
    find_spec: Callable[[str], object | None] | None = None,
) -> PreflightCheck:
    if not selected:
        return PreflightCheck(
            name=name,
            kind="python_import",
            required=False,
            status="skipped",
            summary=f"{name} is not selected for this preflight.",
        )
    finder = find_spec if find_spec is not None else importlib.util.find_spec
    found = finder(name) is not None
    if found:
        return PreflightCheck(
            name=name,
            kind="python_import",
            required=required,
            status="passed",
            summary=f"{name} import is available.",
        )
    status = "failed" if required else "warning"
    return PreflightCheck(
        name=name,
        kind="python_import",
        required=required,
        status=status,
        summary=f"{name} import is missing.",
    )


def check_cli_tool(
    name: str,
    *,
    required: bool,
    selected: bool = True,
    which: Callable[[str], str | None] | None = None,
    extra_paths: Sequence[Path] = (),
) -> PreflightCheck:
    if not selected:
        return PreflightCheck(
            name=name,
            kind="cli_tool",
            required=False,
            status="skipped",
            summary=f"{name} is not selected for this preflight.",
        )
    finder = which if which is not None else shutil.which
    discovered = finder(name)
    if discovered:
        return PreflightCheck(
            name=name,
            kind="cli_tool",
            required=required,
            status="passed",
            summary=f"{name} executable is available.",
            detail=discovered,
        )
    for path in extra_paths:
        if path.is_file() and os.access(path, os.X_OK):
            return PreflightCheck(
                name=name,
                kind="cli_tool",
                required=required,
                status="passed",
                summary=f"{name} executable is available at a known path.",
                detail=str(path),
            )
    status = "failed" if required else "warning"
    return PreflightCheck(
        name=name,
        kind="cli_tool",
        required=required,
        status=status,
        summary=f"{name} executable is missing.",
    )


def summarize_checks(checks: Sequence[PreflightCheck]) -> tuple[bool, str, tuple[str, ...], tuple[str, ...]]:
    missing_required = tuple(
        check.name for check in checks if check.required and check.status == "failed"
    )
    missing_optional = tuple(
        check.name for check in checks if not check.required and check.status == "warning"
    )
    if missing_required:
        return False, "failed", missing_required, missing_optional
    if any(check.status == "warning" for check in checks):
        return True, "warning", missing_required, missing_optional
    return True, "passed", missing_required, missing_optional


def run_preflight(
    *,
    validators: str | Sequence[str] | None = None,
    executor: str = "manual",
    strict: bool = False,
    require_openspec: bool = False,
    require_pytest: bool | None = None,
    require_fastapi: bool = False,
    require_uvicorn: bool = False,
    require_pi: bool = False,
    require_opencode: bool = False,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    find_spec: Callable[[str], object | None] | None = None,
    which: Callable[[str], str | None] | None = None,
    pi_paths: Sequence[Path] = KNOWN_PI_PATHS,
) -> PreflightResult:
    normalized_validators = normalize_validators(validators)
    normalized_executor = executor.strip().lower() if executor.strip() else "manual"
    python_environment = build_python_environment(
        repo_root=repo_root,
        environ=environ,
    )

    pytest_selected = "pytest" in normalized_validators
    openspec_selected = "openspec" in normalized_validators
    pytest_required = pytest_selected if require_pytest is None else require_pytest
    pi_selected = normalized_executor == "pi" or require_pi
    opencode_selected = normalized_executor == "opencode" or require_opencode

    checks = (
        check_python_environment(python_environment, strict=strict),
        check_python_import(
            "pytest",
            required=pytest_required,
            selected=pytest_selected,
            find_spec=find_spec,
        ),
        check_python_import(
            "fastapi",
            required=require_fastapi,
            selected=True,
            find_spec=find_spec,
        ),
        check_python_import(
            "uvicorn",
            required=require_uvicorn,
            selected=True,
            find_spec=find_spec,
        ),
        check_cli_tool(
            "openspec",
            required=require_openspec,
            selected=openspec_selected,
            which=which,
        ),
        check_cli_tool(
            "pi",
            required=require_pi,
            selected=pi_selected,
            which=which,
            extra_paths=pi_paths,
        ),
        check_cli_tool(
            "opencode",
            required=require_opencode,
            selected=opencode_selected,
            which=which,
        ),
    )
    ok, status, missing_required, missing_optional = summarize_checks(checks)
    return PreflightResult(
        ok=ok,
        status=status,
        strict=strict,
        executor=normalized_executor,
        validators=normalized_validators,
        python=python_environment,
        checks=checks,
        missing_required=missing_required,
        missing_optional=missing_optional,
    )


__all__ = [
    "DEFAULT_VALIDATORS",
    "PreflightCheck",
    "PreflightReport",
    "PreflightResult",
    "build_python_environment",
    "check_cli_tool",
    "check_python_environment",
    "check_python_import",
    "normalize_validators",
    "run_preflight",
    "summarize_checks",
]
