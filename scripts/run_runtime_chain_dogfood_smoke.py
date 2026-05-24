#!/usr/bin/env python3
"""Runtime-chain dogfood smoke (Phase E).

This smoke proves that the runtime chain assembled by Phase A through
Phase D is reproducible end-to-end on a fresh queued task. The chain it
exercises is:

  fresh queued TaskRecord
  + real Task Execution Package
  -> real scheduler proposal (confirmed mode)
  -> real scheduler confirmation (confirmed mode)
  -> real intake-runner handoff (confirmed mode)
     + persisted verifier report artifact
  -> real queued_task_handoff (confirmed mode)
     + runtime preflight rechecks proposal_hash / item_hash / TTL
     + writes runtime_preflight_finished / runtime_execution_started /
       runtime_execution_finished events
     + writes runtime_handoff_execution artifact
  -> approved_task_runner with INJECTED fake executor + fake validator
  -> task reaches waiting_approval
  -> read-only API readback via create_app(db_path=...)
     + GET /api/tasks/{task_key}/runtime-audits
     + GET /api/tasks/{task_key}/artifacts
     + GET /api/tasks/{task_key}/validations

Phase E intentionally adds NO new automation. It is NOT a scheduler
loop, NOT a background worker, does NOT pick tasks automatically, does
NOT batch executions, does NOT push branches, does NOT create PRs,
does NOT merge, does NOT approve, does NOT reject, does NOT clean up
real branches or worktrees, and does NOT mutate GitHub. The smoke runs
entirely against a temporary workspace + temporary SQLite DB +
temporary artifact root; the real ``~/.agent-taskflow/state.db`` is
never touched.

Runtime audit evidence (runtime_preflight_finished /
runtime_execution_started / runtime_execution_finished events and the
runtime_handoff_execution artifact) is observation only. It is NOT
action evidence and NOT validation authority; ``validation_result``
events remain the authoritative validator record.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from agent_taskflow.api.main import create_app  # noqa: E402
from agent_taskflow.executors.base import (  # noqa: E402
    Executor,
    ExecutorContext,
    ExecutorResult,
)
from agent_taskflow.intake_runner_handoff import (  # noqa: E402
    IntakeRunnerHandoffRequest,
    create_intake_runner_handoff,
)
from agent_taskflow.models import TaskRecord  # noqa: E402
from agent_taskflow.queued_task_handoff import (  # noqa: E402
    APPROVED_TASK_STATUS,
    INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND,
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
    RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    QueuedTaskHandoffRequest,
    run_queued_task_handoff,
)
from agent_taskflow.scheduler_confirmations import (  # noqa: E402
    SchedulerConfirmationRequest,
    create_scheduler_confirmation,
)
from agent_taskflow.scheduler_proposals import (  # noqa: E402
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_execution_package import (  # noqa: E402
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)
from agent_taskflow.validators.base import (  # noqa: E402
    Validator,
    ValidatorContext,
    ValidatorResult,
)


DEFAULT_TASK_KEY = "RUNTIME-SMOKE-0001"
DEFAULT_PROJECT = "agent-taskflow"
DEFAULT_BOARD = "agent-taskflow"
DEFAULT_BASE_BRANCH = "main"

EXECUTOR_NAME = "noop"  # registered SUPPORTED_EXECUTORS slot; the fake
                       # below overrides it via executor_registry.
VALIDATOR_NAME = "fake-runtime-chain-smoke-validator"

FAKE_MARKER_RELATIVE = "docs/fake-runtime-chain-smoke.md"
FAKE_MARKER_CONTENT = (
    "# Fake Runtime Chain Smoke Marker\n"
    "\n"
    "This file was written by the Phase E runtime-chain dogfood smoke's\n"
    "fake executor. It exists only to give the smoke validator a\n"
    "deterministic local artifact to verify. The smoke does not run any\n"
    "real executor and does not touch GitHub.\n"
)
FAKE_EXECUTOR_LOG_NAME = "fake-runtime-chain-smoke-executor.log"
FAKE_VALIDATOR_LOG_NAME = "fake-runtime-chain-smoke-validator.log"


class SmokeFailure(RuntimeError):
    """Raised when the runtime-chain dogfood smoke fails an invariant."""


# --------------------------------------------------------------------- fake executor


class FakeRuntimeChainExecutor(Executor):
    """In-process executor that writes one deterministic worktree file."""

    name = EXECUTOR_NAME

    def run(self, context: ExecutorContext) -> ExecutorResult:
        if not context.worktree_path.is_dir():
            return ExecutorResult(
                executor=self.name,
                status="blocked",
                exit_code=1,
                summary=f"Prepared worktree does not exist: {context.worktree_path}",
            )

        marker_path = context.worktree_path / FAKE_MARKER_RELATIVE
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(FAKE_MARKER_CONTENT, encoding="utf-8")

        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = context.artifact_dir / FAKE_EXECUTOR_LOG_NAME
        log_path.write_text(
            f"Fake runtime chain executor wrote {marker_path}\n",
            encoding="utf-8",
        )

        return ExecutorResult(
            executor=self.name,
            status="completed",
            exit_code=0,
            log_path=log_path,
            summary="Fake runtime chain executor completed.",
            artifacts={"marker": marker_path, "log": log_path},
        )


# --------------------------------------------------------------------- fake validator


class FakeRuntimeChainValidator(Validator):
    """In-process validator that verifies the executor's marker file."""

    name = VALIDATOR_NAME

    def run(self, context: ValidatorContext) -> ValidatorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = context.artifact_dir / FAKE_VALIDATOR_LOG_NAME
        marker_path = context.worktree_path / FAKE_MARKER_RELATIVE

        failures: list[str] = []
        if not context.worktree_path.is_dir():
            failures.append(f"prepared worktree missing: {context.worktree_path}")
        if not marker_path.is_file():
            failures.append(f"executor marker missing: {marker_path}")
        elif marker_path.read_text(encoding="utf-8") != FAKE_MARKER_CONTENT:
            failures.append("executor marker content mismatch")

        if failures:
            summary = "; ".join(failures)
            log_path.write_text(summary + "\n", encoding="utf-8")
            return ValidatorResult(
                validator=self.name,
                status="failed",
                exit_code=1,
                log_path=log_path,
                summary=summary,
                artifacts={"log": log_path},
            )

        summary = "Fake runtime chain smoke validator verified executor marker."
        log_path.write_text(summary + "\n", encoding="utf-8")
        return ValidatorResult(
            validator=self.name,
            status="passed",
            exit_code=0,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path},
        )


# --------------------------------------------------------------------- helpers


def _run_git(repo_path: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise SmokeFailure(
            f"git {' '.join(args)} failed with {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _init_git_repo(repo_path: Path, base_branch: str) -> str:
    repo_path.mkdir(parents=True, exist_ok=True)
    _run_git(repo_path, ["init"])
    _run_git(repo_path, ["config", "user.email", "agent-taskflow@example.invalid"])
    _run_git(repo_path, ["config", "user.name", "Agent Taskflow Runtime Smoke"])
    (repo_path / "README.md").write_text(
        "# runtime-chain dogfood smoke\n", encoding="utf-8"
    )
    _run_git(repo_path, ["add", "README.md"])
    _run_git(repo_path, ["commit", "-m", "initial"])
    _run_git(repo_path, ["branch", "-M", base_branch])
    return _run_git(repo_path, ["rev-parse", base_branch])


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


@dataclass(frozen=True)
class _ChainPaths:
    workspace_root: Path
    db_path: Path
    repo_path: Path
    artifact_root: Path
    worktree_root: Path


def _prepare_chain_paths(workspace_root: Path) -> _ChainPaths:
    workspace_root.mkdir(parents=True, exist_ok=True)
    repo_path = workspace_root / "repo"
    # workspace_manager enforces that worktree_root is inside
    # repo_path/.worktrees.
    return _ChainPaths(
        workspace_root=workspace_root,
        db_path=workspace_root / "runtime-chain-dogfood-smoke.db",
        repo_path=repo_path,
        artifact_root=workspace_root / "artifacts",
        worktree_root=repo_path / ".worktrees",
    )


def _seed_queued_task(
    store: TaskMirrorStore,
    *,
    task_key: str,
    repo_path: Path,
    artifact_root: Path,
) -> Path:
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir(parents=True, exist_ok=True)
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=DEFAULT_PROJECT,
            board=DEFAULT_BOARD,
            hermes_task_id=f"t_{task_key.lower()}",
            title=f"Runtime chain smoke task {task_key}",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
        )
    )
    return artifact_dir


def _select_queued_handoff_item(proposal: dict[str, Any], task_key: str) -> str:
    items = proposal.get("items")
    if not isinstance(items, list):
        raise SmokeFailure(
            f"scheduler proposal missing items list: {proposal!r}"
        )
    matching = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("task_key") == task_key
        and item.get("recommended_command_kind")
        == INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
    ]
    if not matching:
        raise SmokeFailure(
            "scheduler proposal did not include a queued_task_handoff item "
            f"for {task_key}; got items={items!r}"
        )
    item_id = matching[0].get("proposal_item_id")
    if not isinstance(item_id, str) or not item_id:
        raise SmokeFailure(
            f"queued_task_handoff item missing proposal_item_id: {matching[0]!r}"
        )
    return item_id


# --------------------------------------------------------------------- main flow


def run_smoke(
    *,
    workspace_root: Path,
    base_branch: str = DEFAULT_BASE_BRANCH,
    task_key: str = DEFAULT_TASK_KEY,
) -> dict[str, Any]:
    """Run the runtime-chain dogfood smoke and return a JSON-safe summary."""

    paths = _prepare_chain_paths(workspace_root)
    base_sha = _init_git_repo(paths.repo_path, base_branch)
    paths.artifact_root.mkdir(parents=True, exist_ok=True)
    paths.worktree_root.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(paths.db_path)
    store.init_db()

    # 1. Seed a queued task directly. Phase E focuses on the runtime chain,
    #    not on issue intake; the issue-to-waiting_approval smoke already
    #    covers the GitHub-intake half of the chain.
    _seed_queued_task(
        store,
        task_key=task_key,
        repo_path=paths.repo_path,
        artifact_root=paths.artifact_root,
    )

    # 2. Real Task Execution Package (confirmed mode).
    package_result = create_task_execution_package(
        TaskExecutionPackageRequest(
            task_key=task_key,
            db_path=paths.db_path,
            artifact_root=paths.artifact_root,
            dry_run=False,
            confirm=True,
        ),
        store=store,
    )
    _require(
        package_result["ok"],
        f"task execution package creation blocked: {package_result.get('error')}",
    )
    package_artifact_dir = Path(package_result["artifact_dir"])
    prompt_path = package_artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
    package_path = package_artifact_dir / PACKAGE_FILENAME
    _require(prompt_path.is_file(), f"implementation_prompt.md missing: {prompt_path}")
    _require(package_path.is_file(), f"task_execution_package.json missing: {package_path}")

    # 3. Real scheduler proposal — should now recommend queued_task_handoff
    #    for the seeded task because the package is present.
    proposal = create_scheduler_proposal(
        SchedulerProposalRequest(
            db_path=paths.db_path,
            artifact_root=paths.artifact_root,
            task_key=task_key,
            dry_run=False,
            confirm_create_proposal=True,
        )
    )
    proposal_artifact_path = proposal.get("artifact_path")
    _require(
        isinstance(proposal_artifact_path, str) and proposal_artifact_path,
        f"scheduler proposal did not produce artifact_path: {proposal!r}",
    )
    _require(
        Path(str(proposal_artifact_path)).is_file(),
        f"scheduler_proposal.json missing on disk: {proposal_artifact_path}",
    )
    proposal_id = proposal.get("proposal_id")
    _require(
        isinstance(proposal_id, str) and bool(proposal_id),
        f"scheduler proposal missing proposal_id: {proposal!r}",
    )
    item_id = _select_queued_handoff_item(proposal, task_key)

    # 4. Real scheduler confirmation for the queued_task_handoff item.
    confirmation = create_scheduler_confirmation(
        SchedulerConfirmationRequest(
            db_path=paths.db_path,
            artifact_root=paths.artifact_root,
            proposal_id=str(proposal_id),
            selected_item_ids=(item_id,),
            dry_run=False,
            confirm_create_confirmation=True,
            confirmed_by="runtime_chain_dogfood_smoke",
        )
    )
    confirmation_artifact_path = confirmation.get("artifact_path")
    _require(
        isinstance(confirmation_artifact_path, str)
        and bool(confirmation_artifact_path),
        f"scheduler confirmation missing artifact_path: {confirmation!r}",
    )
    _require(
        Path(str(confirmation_artifact_path)).is_file(),
        f"scheduler_confirmation.json missing on disk: {confirmation_artifact_path}",
    )

    # 5. Real intake-runner handoff (confirmed mode). This persists both
    #    the handoff artifact and the verifier report artifact on disk.
    handoff = create_intake_runner_handoff(
        IntakeRunnerHandoffRequest(
            db_path=paths.db_path,
            artifact_root=paths.artifact_root,
            proposal_item_id=item_id,
            confirmation_id=str(confirmation["confirmation_id"]),
            task_key=task_key,
            expected_command_kind=(
                INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
            ),
            dry_run=False,
            confirm_create_handoff=True,
        )
    )
    _require(handoff["ok"], f"intake_runner_handoff not ok: {handoff.get('error')}")
    handoff_artifact_path = handoff.get("artifact_path")
    _require(
        isinstance(handoff_artifact_path, str) and bool(handoff_artifact_path),
        f"intake_runner_handoff missing artifact_path: {handoff!r}",
    )
    _require(
        Path(str(handoff_artifact_path)).is_file(),
        f"intake_runner_handoff.json missing on disk: {handoff_artifact_path}",
    )
    verifier_report_block = handoff.get("verifier_report") or {}
    verifier_report_path = verifier_report_block.get("verifier_report_path")
    verifier_run_id = verifier_report_block.get("verifier_run_id")
    _require(
        isinstance(verifier_report_path, str) and bool(verifier_report_path),
        f"intake_runner_handoff missing verifier_report_path: {handoff!r}",
    )
    _require(
        Path(str(verifier_report_path)).is_file(),
        f"verifier report artifact missing on disk: {verifier_report_path}",
    )

    # 6. Real queued_task_handoff in confirmed mode with the real handoff
    #    artifact. The runtime preflight inside queued_task_handoff
    #    re-opens both the handoff artifact and the verifier report and
    #    rechecks proposal_hash / item_hash / TTL before invoking the
    #    runner. We inject a fake executor + fake validator so the smoke
    #    is hermetic (no real Pi/OpenCode/network).
    runtime_handoff = run_queued_task_handoff(
        QueuedTaskHandoffRequest(
            task_key=task_key,
            executor=EXECUTOR_NAME,
            repo_path=paths.repo_path,
            db_path=paths.db_path,
            artifact_root=paths.artifact_root,
            worktree_root=paths.worktree_root,
            base_branch=base_branch,
            validators=(VALIDATOR_NAME,),
            preflight=False,
            dry_run=False,
            confirm_handoff=True,
            intake_runner_handoff_artifact_path=Path(str(handoff_artifact_path)),
        ),
        store=store,
        executor_registry={EXECUTOR_NAME: FakeRuntimeChainExecutor()},
        validator_registry={VALIDATOR_NAME: FakeRuntimeChainValidator()},
    )
    runtime_handoff_dict = runtime_handoff.to_dict()
    _require(
        runtime_handoff_dict["ok"] is True,
        f"queued_task_handoff was not ok: {runtime_handoff_dict.get('error')}",
    )
    _require(
        runtime_handoff_dict["status"] == APPROVED_TASK_STATUS,
        f"queued handoff status {runtime_handoff_dict['status']!r} "
        f"!= {APPROVED_TASK_STATUS!r}",
    )
    runtime_block = runtime_handoff_dict.get("runtime") or {}
    runtime_execution_artifact_path = runtime_block.get(
        "runtime_execution_artifact_path"
    )
    _require(
        isinstance(runtime_execution_artifact_path, str)
        and bool(runtime_execution_artifact_path),
        f"runtime audit artifact path missing: {runtime_block!r}",
    )
    _require(
        Path(str(runtime_execution_artifact_path)).is_file(),
        f"runtime_handoff_execution artifact missing on disk: "
        f"{runtime_execution_artifact_path}",
    )

    # 7. Verify runtime audit DB events.
    runtime_audit_events = store.list_runtime_audit_events(task_key)
    runtime_event_kinds = sorted({event["kind"] for event in runtime_audit_events})
    expected_kinds = {
        RUNTIME_PREFLIGHT_EVENT_TYPE,
        RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
        RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
    }
    missing_kinds = expected_kinds.difference(runtime_event_kinds)
    _require(
        not missing_kinds,
        f"runtime audit DB events missing kinds {sorted(missing_kinds)}; "
        f"observed={runtime_event_kinds}",
    )

    # 8. Verify validator evidence and waiting_approval state.
    final_task = store.get_task(task_key)
    _require(final_task is not None, "task missing after handoff")
    assert final_task is not None
    _require(
        final_task.status == APPROVED_TASK_STATUS,
        f"final task status {final_task.status!r} != {APPROVED_TASK_STATUS!r}",
    )
    validation_results = store.list_validation_results(task_key)
    _require(
        len(validation_results) >= 1,
        f"no validation_result events recorded: {validation_results!r}",
    )
    _require(
        any(result.get("status") == "passed" for result in validation_results),
        "no validator result reached status=passed; runtime audit is not "
        "a substitute for validator results",
    )

    # 9. Verify runtime audit artifact was recorded as a DB artifact.
    db_artifacts = store.list_task_artifacts(task_key)
    runtime_artifact_records = [
        record
        for record in db_artifacts
        if record.artifact_type == RUNTIME_EXECUTION_ARTIFACT_TYPE
    ]
    _require(
        len(runtime_artifact_records) >= 1,
        "runtime_handoff_execution artifact not recorded in store",
    )

    # 10. Verify runtime_handoff_execution artifact safety block.
    runtime_artifact_payload = json.loads(
        Path(str(runtime_execution_artifact_path)).read_text(encoding="utf-8")
    )
    safety_block = runtime_artifact_payload.get("safety") or {}
    required_safety_true = ("runtime_audit_only", "not_action_evidence", "not_validation_authority")
    required_safety_false = (
        "approved",
        "merged",
        "cleanup_performed",
        "background_worker_started",
    )
    for flag in required_safety_true:
        _require(
            safety_block.get(flag) is True,
            f"runtime_handoff_execution safety flag {flag!r} must be True; "
            f"observed safety={safety_block!r}",
        )
    for flag in required_safety_false:
        _require(
            safety_block.get(flag) is False,
            f"runtime_handoff_execution safety flag {flag!r} must be False; "
            f"observed safety={safety_block!r}",
        )

    # 11. API readback via the FastAPI test client. Phase D exposes the
    #     runtime audit readback endpoint, and validators remain
    #     authoritative through the existing /validations endpoint.
    api_readback = _read_through_api(paths.db_path, task_key)
    _require(
        api_readback["runtime_audits_count"] >= 3,
        "API /runtime-audits did not return at least three events: "
        f"{api_readback!r}",
    )
    api_kinds = set(api_readback["runtime_audit_kinds"])
    missing_api_kinds = expected_kinds.difference(api_kinds)
    _require(
        not missing_api_kinds,
        f"API /runtime-audits missing runtime kinds "
        f"{sorted(missing_api_kinds)}; observed={sorted(api_kinds)}",
    )
    for item in api_readback["runtime_audit_items"]:
        _require(
            item.get("not_action_evidence") is True,
            f"API runtime audit item missing not_action_evidence=true: {item!r}",
        )
        _require(
            item.get("not_validation_authority") is True,
            f"API runtime audit item missing not_validation_authority=true: "
            f"{item!r}",
        )
    api_artifact_types = api_readback["artifact_types"]
    _require(
        RUNTIME_EXECUTION_ARTIFACT_TYPE in api_artifact_types,
        f"API /artifacts did not expose runtime_handoff_execution: "
        f"{api_artifact_types!r}",
    )
    _require(
        api_readback["validations_count"] >= 1,
        f"API /validations did not return validator records: {api_readback!r}",
    )

    return {
        "ok": True,
        "task_key": task_key,
        "final_status": final_task.status,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "db_path": str(paths.db_path),
        "repo_path": str(paths.repo_path),
        "artifact_root": str(paths.artifact_root),
        "package": {
            "artifact_dir": str(package_artifact_dir),
            "implementation_prompt_path": str(prompt_path),
            "package_path": str(package_path),
        },
        "scheduler": {
            "proposal_id": proposal_id,
            "proposal_artifact_path": str(proposal_artifact_path),
            "confirmation_id": confirmation.get("confirmation_id"),
            "confirmation_artifact_path": str(confirmation_artifact_path),
            "proposal_item_id": item_id,
        },
        "intake_runner_handoff": {
            "artifact_path": str(handoff_artifact_path),
            "verifier_run_id": verifier_run_id,
            "verifier_report_path": str(verifier_report_path),
            "recommended_command_kind": (
                INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
            ),
        },
        "runtime_audit": {
            "runtime_execution_id": runtime_block.get("runtime_execution_id"),
            "runtime_execution_artifact_path": str(
                runtime_execution_artifact_path
            ),
            "runtime_event_count": len(runtime_audit_events),
            "runtime_event_kinds": runtime_event_kinds,
            "preflight_event_recorded": runtime_block.get(
                "runtime_preflight_event_recorded"
            ),
            "execution_started_event_recorded": runtime_block.get(
                "runtime_execution_started_event_recorded"
            ),
            "execution_finished_event_recorded": runtime_block.get(
                "runtime_execution_finished_event_recorded"
            ),
            "not_action_evidence": True,
            "not_validation_authority": True,
        },
        "validation": {
            "validation_result_count": len(validation_results),
            "passed_validation_count": sum(
                1
                for result in validation_results
                if result.get("status") == "passed"
            ),
        },
        "api_readback": api_readback,
        "safety": {
            "local_only": True,
            "used_real_executor": False,
            "network_used": False,
            "github_mutated": False,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "rejected": False,
            "cleanup_performed": False,
            "background_worker_started": False,
            "scheduler_loop_started": False,
            "auto_selected_task": False,
            "batch_execution": False,
            "production_db_mutated": False,
            "runtime_audit_is_validation_authority": False,
        },
    }


def _read_through_api(db_path: Path, task_key: str) -> dict[str, Any]:
    """Read runtime audit / validation / artifact data via the API.

    Uses the existing read-only FastAPI app pointed at the temporary
    smoke DB. The API exposes no runtime action endpoints; this call
    never mutates the DB.
    """
    app = create_app(db_path)
    with TestClient(app) as client:
        runtime_response = client.get(
            f"/api/tasks/{task_key}/runtime-audits"
        )
        if runtime_response.status_code != 200:
            raise SmokeFailure(
                f"GET /api/tasks/{task_key}/runtime-audits returned "
                f"{runtime_response.status_code}: {runtime_response.text}"
            )
        runtime_payload = runtime_response.json()

        artifacts_response = client.get(f"/api/tasks/{task_key}/artifacts")
        if artifacts_response.status_code != 200:
            raise SmokeFailure(
                f"GET /api/tasks/{task_key}/artifacts returned "
                f"{artifacts_response.status_code}: {artifacts_response.text}"
            )
        artifacts_payload = artifacts_response.json()

        validations_response = client.get(
            f"/api/tasks/{task_key}/validations"
        )
        if validations_response.status_code != 200:
            raise SmokeFailure(
                f"GET /api/tasks/{task_key}/validations returned "
                f"{validations_response.status_code}: {validations_response.text}"
            )
        validations_payload = validations_response.json()

    runtime_items = runtime_payload.get("items") or []
    artifact_items = artifacts_payload.get("items") or []
    validation_items = validations_payload.get("items") or []
    return {
        "runtime_audits_count": int(runtime_payload.get("count") or 0),
        "runtime_audit_kinds": sorted(
            {item.get("kind") for item in runtime_items if item.get("kind")}
        ),
        "runtime_audit_items": runtime_items,
        "artifact_types": sorted(
            {
                item.get("artifact_type")
                for item in artifact_items
                if item.get("artifact_type")
            }
        ),
        "artifact_count": int(artifacts_payload.get("count") or 0),
        "validations_count": int(validations_payload.get("count") or 0),
    }


# --------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Phase E runtime-chain dogfood smoke: scheduler proposal -> "
            "confirmation -> intake-runner handoff -> queued_task_handoff "
            "(with runtime audit events) -> approved_task_runner -> "
            "waiting_approval -> read-only API readback. Local-only, "
            "fake-executor, no network, no GitHub mutation."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root. If omitted, a temporary directory "
            "under $TMPDIR is created and removed after the smoke unless "
            "--keep-temp is supplied."
        ),
    )
    parser.add_argument(
        "--task-key",
        default=DEFAULT_TASK_KEY,
        help=f"Task key to use for the smoke. Default: {DEFAULT_TASK_KEY}.",
    )
    parser.add_argument(
        "--base-branch",
        default=DEFAULT_BASE_BRANCH,
        help=f"Base branch for the temp git repo. Default: {DEFAULT_BASE_BRANCH}.",
    )
    parser.add_argument(
        "--executor",
        default=EXECUTOR_NAME,
        help=(
            "Executor slot name (fixed; the smoke always uses an injected "
            "fake executor regardless of this value)."
        ),
    )
    parser.add_argument(
        "--artifact-root",
        help=(
            "Ignored unless --workspace-root is also supplied. The smoke "
            "writes all artifacts under <workspace-root>/artifacts."
        ),
    )
    parser.add_argument(
        "--db-path",
        help=(
            "Ignored unless --workspace-root is also supplied. The smoke "
            "writes state to <workspace-root>/runtime-chain-dogfood-smoke.db."
        ),
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help=(
            "Do not delete the temporary workspace after the smoke "
            "completes. Useful for operator inspection."
        ),
    )
    parser.add_argument(
        "--cleanup-temp",
        action="store_true",
        help=(
            "Force temp workspace cleanup after the smoke completes "
            "(default unless --keep-temp is supplied)."
        ),
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit compact JSON.")
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (default when --json is omitted).",
    )
    return parser


def _emit(payload: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    compact = bool(args.json) and not bool(args.pretty)

    if args.workspace_root:
        workspace_root = Path(args.workspace_root).expanduser()
        if not workspace_root.is_absolute():
            _emit(
                {
                    "ok": False,
                    "error": f"--workspace-root must be absolute: {args.workspace_root}",
                    "safety": _cli_error_safety(),
                },
                compact=compact,
            )
            return 2
        provided_workspace = True
    else:
        workspace_root = Path(
            tempfile.mkdtemp(prefix="agent-taskflow-runtime-chain-dogfood-smoke-")
        )
        provided_workspace = False

    keep_workspace = bool(args.keep_temp or provided_workspace)

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            base_branch=args.base_branch,
            task_key=args.task_key,
        )
    except SmokeFailure as exc:
        summary = {
            "ok": False,
            "error": str(exc),
            "workspace_root": str(workspace_root),
            "safety": _cli_error_safety(),
        }
        _emit(summary, compact=compact)
        if not keep_workspace:
            _try_remove(workspace_root)
        return 1
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        summary = {
            "ok": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "workspace_root": str(workspace_root),
            "safety": _cli_error_safety(),
        }
        _emit(summary, compact=compact)
        if not keep_workspace:
            _try_remove(workspace_root)
        return 1

    summary["workspace_root"] = str(workspace_root)
    summary["workspace_kept"] = keep_workspace
    _emit(summary, compact=compact)

    if not keep_workspace:
        _try_remove(workspace_root)

    return 0 if summary.get("ok") else 1


def _cli_error_safety() -> dict[str, object]:
    return {
        "local_only": True,
        "used_real_executor": False,
        "network_used": False,
        "github_mutated": False,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "rejected": False,
        "cleanup_performed": False,
        "background_worker_started": False,
        "scheduler_loop_started": False,
        "auto_selected_task": False,
        "batch_execution": False,
        "production_db_mutated": False,
        "runtime_audit_is_validation_authority": False,
    }


def _try_remove(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:  # pragma: no cover - cleanup is best-effort
        pass


if __name__ == "__main__":
    raise SystemExit(main())
