"""Integration validator runner and evidence persistence (§29).

Taskflow validators are the deterministic technical gate between
``integrating`` and ``needs_review``. This runner executes the configured
validators inside the task worktree and persists, for every one of them, the
§29 evidence set: name, command, output, branch SHA, target SHA and diff
context.

Two deliberate choices:

* An empty validator set does **not** pass. A gate that gates nothing would
  let any change reach ``needs_review`` unchecked.
* A validator whose binary is missing **fails**; it is not skipped. A gate
  that silently disappears when a tool is absent is not a gate.

The runner never retries and never classifies a failure — §29.1 fixes the
outcome of a red validator at ``needs_decision``, which the controller applies.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import utc_now_iso
from agent_taskflow.tasks import normalize_task_key


__all__ = [
    "ARTIFACT_TYPE",
    "IntegrationValidationReport",
    "IntegrationValidatorOutcome",
    "IntegrationValidatorSpec",
    "run_integration_validators",
]


ARTIFACT_TYPE = "integration_validator_evidence"
SOURCE = "integration_validators"

MAX_CAPTURED_OUTPUT_CHARS = 20000


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class IntegrationValidatorSpec:
    """One validator to run during integration."""

    name: str
    command: tuple[str, ...]
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        normalized = self.name.strip()
        if not normalized:
            raise ValueError("validator name must not be empty")
        object.__setattr__(self, "name", normalized)
        command = tuple(str(part) for part in self.command)
        if not command:
            raise ValueError(f"validator {normalized!r} needs a command")
        object.__setattr__(self, "command", command)


@dataclass(frozen=True)
class IntegrationValidatorOutcome:
    """The result of one validator, with its full §29 evidence."""

    name: str
    command: tuple[str, ...]
    status: str
    exit_code: int | None
    output: str
    branch_sha: str | None
    target_sha: str | None
    diff_context: str | None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator": self.name,
            "command": list(self.command),
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "branch_sha": self.branch_sha,
            "target_sha": self.target_sha,
            "diff_context": self.diff_context,
        }


@dataclass(frozen=True)
class IntegrationValidationReport:
    """The gate decision for one integration run."""

    task_key: str
    integration_run_id: str
    passed: bool
    outcomes: tuple[IntegrationValidatorOutcome, ...]
    summary: str
    evidence_path: Path | None = None

    @property
    def failed_names(self) -> list[str]:
        return [outcome.name for outcome in self.outcomes if not outcome.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ARTIFACT_TYPE,
            "artifact_type": ARTIFACT_TYPE,
            "task_key": self.task_key,
            "integration_run_id": self.integration_run_id,
            "passed": self.passed,
            "failed": self.failed_names,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "summary": self.summary,
            "generated_at": utc_now_iso(),
        }


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURED_OUTPUT_CHARS:
        return text
    kept = MAX_CAPTURED_OUTPUT_CHARS // 2
    return f"{text[:kept]}\n...[truncated]...\n{text[-kept:]}"


def _default_runner(argv: Sequence[str], cwd: Path, timeout: int | None) -> CompletedProcessLike:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _run_one(
    spec: IntegrationValidatorSpec,
    *,
    worktree_path: Path,
    runner: Runner | None,
    branch_sha: str | None,
    target_sha: str | None,
    diff_context: str | None,
) -> IntegrationValidatorOutcome:
    execute = runner or _default_runner
    try:
        completed = execute(spec.command, worktree_path, spec.timeout_seconds)
        exit_code = completed.returncode
        output = _truncate(f"{completed.stdout or ''}{completed.stderr or ''}")
    except FileNotFoundError as exc:
        # A missing validator binary is a red gate, never a skip.
        exit_code = 127
        output = f"validator command could not be executed: {exc}"
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        output = f"validator timed out after {exc.timeout}s"
    except OSError as exc:  # pragma: no cover - defensive
        exit_code = 126
        output = f"validator command failed to start: {exc}"

    return IntegrationValidatorOutcome(
        name=spec.name,
        command=spec.command,
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        output=output,
        branch_sha=branch_sha,
        target_sha=target_sha,
        diff_context=diff_context,
    )


def run_integration_validators(
    *,
    task_key: str,
    worktree_path: Path,
    artifact_dir: Path | None,
    specs: Sequence[IntegrationValidatorSpec],
    branch_sha: str | None,
    target_sha: str | None,
    diff_context: str | None,
    integration_run_id: str,
    integration_store: IntegrationStore,
    runner: Runner | None = None,
) -> IntegrationValidationReport:
    """Run every validator, persist §29 evidence, and return the gate decision."""
    key = normalize_task_key(task_key)

    if not specs:
        report = IntegrationValidationReport(
            task_key=key,
            integration_run_id=integration_run_id,
            passed=False,
            outcomes=(),
            summary=(
                "No validators are configured for this integration; a gate that "
                "gates nothing cannot admit a Ticket to needs_review."
            ),
        )
        return _persist(report, artifact_dir, integration_store, key)

    outcomes = tuple(
        _run_one(
            spec,
            worktree_path=Path(worktree_path),
            runner=runner,
            branch_sha=branch_sha,
            target_sha=target_sha,
            diff_context=diff_context,
        )
        for spec in specs
    )

    for outcome in outcomes:
        integration_store.record_validator_evidence(
            key,
            integration_run_id=integration_run_id,
            validator=outcome.name,
            command=outcome.command,
            status=outcome.status,
            exit_code=outcome.exit_code,
            output=outcome.output,
            branch_sha=outcome.branch_sha,
            target_sha=outcome.target_sha,
            diff_context=outcome.diff_context,
        )

    passed = all(outcome.passed for outcome in outcomes)
    failed = [outcome.name for outcome in outcomes if not outcome.passed]
    summary = (
        f"{len(outcomes)} validator(s) passed"
        if passed
        else f"Validators failed: {', '.join(failed)}"
    )
    report = IntegrationValidationReport(
        task_key=key,
        integration_run_id=integration_run_id,
        passed=passed,
        outcomes=outcomes,
        summary=summary,
    )
    return _persist(report, artifact_dir, integration_store, key)


def _persist(
    report: IntegrationValidationReport,
    artifact_dir: Path | None,
    integration_store: IntegrationStore,
    task_key: str,
) -> IntegrationValidationReport:
    if artifact_dir is None:
        return report

    directory = Path(artifact_dir) / "integration"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"validators-{report.integration_run_id}.json"
    atomic_write_json(path, report.to_dict(), sort_keys=True)
    integration_store.task_store.record_task_artifact(task_key, ARTIFACT_TYPE, path)

    return IntegrationValidationReport(
        task_key=report.task_key,
        integration_run_id=report.integration_run_id,
        passed=report.passed,
        outcomes=report.outcomes,
        summary=report.summary,
        evidence_path=path,
    )
