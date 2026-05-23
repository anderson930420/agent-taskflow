#!/usr/bin/env python3
"""Local issue-to-waiting_approval golden-path smoke.

This smoke proves the full local chain works end-to-end:

  offline GitHub Issue JSON fixture
  -> deterministic Phase 6D issue intake
  -> queued TaskRecord
  -> Phase 6E Task Execution Package (implementation_prompt.md +
     task_execution_package.json + store artifacts + event)
  -> Phase 6E+1 explicit queued-task handoff (--confirm-handoff)
  -> approved_task_runner with INJECTED fake executor and fake
     validator (no Pi, no OpenCode, no real external AI, no
     network)
  -> task reaches waiting_approval
  -> evidence is recorded in TaskMirrorStore for operator review

This is a local acceptance smoke. It is NOT a scheduler, NOT a
background loop, does NOT touch GitHub, does NOT push, does NOT
create a PR, does NOT merge, does NOT approve, and does NOT clean
up real repo branches or worktrees.
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

from datetime import datetime, timezone  # noqa: E402

from agent_taskflow.executors.base import (  # noqa: E402
    Executor,
    ExecutorContext,
    ExecutorResult,
)
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot  # noqa: E402
from agent_taskflow.github_issue_intake_gate import (  # noqa: E402
    GitHubIssueIntakeRequest,
    intake_selected_github_issues,
)
from agent_taskflow.intake_runner_handoff import (  # noqa: E402
    SCHEMA_VERSION as INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
    STATUS_CREATED as INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
    VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
)
from agent_taskflow.queued_task_handoff import (  # noqa: E402
    APPROVED_TASK_STATUS,
    INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND,
    QueuedTaskHandoffRequest,
    run_queued_task_handoff,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_execution_package import (  # noqa: E402
    EVENT_TYPE as PACKAGE_EVENT_TYPE,
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_ARTIFACT_TYPE,
    PACKAGE_FILENAME,
    PROMPT_ARTIFACT_TYPE,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)
from agent_taskflow.validators.base import (  # noqa: E402
    Validator,
    ValidatorContext,
    ValidatorResult,
)


DEFAULT_REPO = "agent-taskflow-smoke/agent-taskflow"
DEFAULT_PROJECT = "agent-taskflow"
DEFAULT_BOARD = "agent-taskflow"
DEFAULT_ISSUE_NUMBER = 9100
DEFAULT_TASK_KEY = f"GH-{DEFAULT_ISSUE_NUMBER}"
DEFAULT_BASE_BRANCH = "main"

EXECUTOR_NAME = "noop"  # registered SUPPORTED_EXECUTORS slot; the fake
                       # below overrides it via executor_registry.
VALIDATOR_NAME = "fake-local-smoke-validator"

FAKE_MARKER_RELATIVE = "docs/fake-local-smoke.md"
FAKE_MARKER_CONTENT = (
    "# Fake Local Smoke Marker\n"
    "\n"
    "This file was written by the issue-to-waiting_approval smoke's\n"
    "fake executor. It exists only to give the smoke validator a\n"
    "deterministic local artifact to verify.\n"
)
FAKE_EXECUTOR_LOG_NAME = "fake-local-smoke-executor.log"
FAKE_VALIDATOR_LOG_NAME = "fake-local-smoke-validator.log"


class SmokeFailure(RuntimeError):
    """Raised when the issue-to-waiting_approval smoke fails an invariant."""


# --------------------------------------------------------------------- fake executor


class FakeLocalSmokeExecutor(Executor):
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
            "Fake local smoke executor wrote {path}\n".format(path=marker_path),
            encoding="utf-8",
        )

        return ExecutorResult(
            executor=self.name,
            status="completed",
            exit_code=0,
            log_path=log_path,
            summary="Fake local smoke executor completed.",
            artifacts={
                "marker": marker_path,
                "log": log_path,
            },
        )


# --------------------------------------------------------------------- fake validator


class FakeLocalSmokeValidator(Validator):
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

        summary = "Fake local smoke validator verified executor marker."
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
    _run_git(repo_path, ["config", "user.name", "Agent Taskflow Smoke"])
    (repo_path / "README.md").write_text(
        "# issue to waiting_approval smoke\n", encoding="utf-8"
    )
    _run_git(repo_path, ["add", "README.md"])
    _run_git(repo_path, ["commit", "-m", "initial"])
    _run_git(repo_path, ["branch", "-M", base_branch])
    return _run_git(repo_path, ["rev-parse", base_branch])


def _build_issue_snapshot(repo: str, issue_number: int) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot.from_json(
        {
            "number": issue_number,
            "title": "Issue-to-waiting_approval smoke issue",
            "body": (
                "Offline issue fixture for the local issue-to-waiting_approval "
                "smoke. The system must build a Task Execution Package and hand "
                "the queued task to approved_task_runner under explicit "
                "--confirm-handoff."
            ),
            "state": "OPEN",
            "labels": [{"name": "smoke"}],
            "author": {"login": "agent-taskflow-smoke"},
            "url": f"https://example.invalid/{repo}/issues/{issue_number}",
            "createdAt": "2026-05-21T00:00:00Z",
            "updatedAt": "2026-05-21T00:00:00Z",
        }
    )


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
    # repo_path/.worktrees, so we keep it there explicitly rather than
    # branching out into a sibling directory of the repo.
    return _ChainPaths(
        workspace_root=workspace_root,
        db_path=workspace_root / "issue-to-waiting-approval-smoke.db",
        repo_path=repo_path,
        artifact_root=workspace_root / "artifacts",
        worktree_root=repo_path / ".worktrees",
    )


def _utc_now_iso_smoke() -> str:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def _write_smoke_intake_runner_handoff_pair(
    *,
    artifact_root: Path,
    db_path: Path,
    task_key: str,
) -> Path:
    """Write a synthetic Phase A handoff + verifier_report pair.

    The smoke does not exercise the full scheduler proposal /
    confirmation pipeline, so this helper produces the on-disk
    artifacts in the exact shape ``create_intake_runner_handoff``
    would have produced in confirmed mode. The pair satisfies every
    check in ``_verify_intake_runner_handoff`` so the smoke can
    continue to reach ``approved_task_runner``.
    """

    now = _utc_now_iso_smoke()
    verifier_run_id = f"verifier-run-smoke-{task_key}"
    handoff_id = f"handoff-smoke-{task_key}"
    expiration = {
        "kind": INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND,
        "default_max_age_minutes": 15,
        "max_age_minutes_override": None,
        "effective_max_age_minutes": 15,
        "max_age_minutes": 15,
        "max_age_source": "default",
        "confirmation_created_at": now,
        "now": now,
        "age_seconds": 0,
        "expired": False,
        "detail": None,
    }
    report = {
        "ok": True,
        "status": "valid",
        "schema_version": "scheduler_confirmation_verifier_report.v1",
        "source": "scheduler_confirmation_verifier",
        "verification_passed": True,
        "eligible_for_command_specific_confirm": True,
        "execution_allowed": False,
        "allowed_to_attempt": False,
        "execution_performed": False,
        "action_evidence_created": False,
        "task_key": task_key,
        "recommended_command_kind": (
            INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
        ),
        "proposal_id": f"proposal-smoke-{task_key}",
        "proposal_hash": f"proposal-hash-smoke-{task_key}",
        "proposal_artifact_path": "/abs/smoke/proposal.json",
        "proposal_item_id": f"proposal-item-smoke-{task_key}",
        "item_hash": f"item-hash-smoke-{task_key}",
        "confirmation_id": f"confirmation-smoke-{task_key}",
        "confirmation_artifact_path": "/abs/smoke/confirmation.json",
        "confirmation_created_at": now,
        "expiration": expiration,
        "checks": [{"name": "smoke", "passed": True}],
        "safety": {
            "verifier_dry_run": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
        },
    }
    verifier_report_path = (
        artifact_root
        / "scheduler_confirmation_verifier_reports"
        / verifier_run_id
        / "verifier_report.json"
    )
    verifier_report_path.parent.mkdir(parents=True, exist_ok=True)
    verifier_report_path.write_text(
        json.dumps(
            {
                "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
                "verifier_run_id": verifier_run_id,
                "created_at": now,
                "source": "intake_runner_handoff",
                "report": report,
                "safety": {
                    "dry_run_report_only": True,
                    "execution_allowed": False,
                    "execution_performed": False,
                    "action_evidence_created": False,
                    "executor_started": False,
                    "validators_started": False,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    handoff_path = (
        artifact_root
        / "intake_runner_handoffs"
        / handoff_id
        / "intake_runner_handoff.json"
    )
    handoff_payload = {
        "ok": True,
        "status": INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
        "schema_version": INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "created_at": now,
        "source": "intake_runner_handoff",
        "mode": "confirmed",
        "db_path": str(db_path),
        "artifact_root": str(artifact_root),
        "artifact_path": str(handoff_path),
        "task_key": task_key,
        "recommended_command_kind": (
            INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
        ),
        "proposal": {
            "proposal_id": report["proposal_id"],
            "proposal_hash": report["proposal_hash"],
            "proposal_artifact_path": report["proposal_artifact_path"],
            "proposal_item_id": report["proposal_item_id"],
            "item_hash": report["item_hash"],
        },
        "confirmation": {
            "confirmation_id": report["confirmation_id"],
            "confirmation_artifact_path": (
                report["confirmation_artifact_path"]
            ),
            "verification_status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
        },
        "runner_contract": {
            "runner_may_start": False,
            "execution_allowed": False,
            "execution_performed": False,
            "executor_started": False,
            "validators_started": False,
            "action_evidence_created": False,
            "requires_future_runtime_gate": True,
        },
        "safety": {
            "handoff_only": True,
            "will_execute": False,
            "will_push": False,
            "will_create_pr": False,
            "will_merge": False,
            "will_approve": False,
            "will_reject": False,
            "will_cleanup": False,
            "will_delete_branch": False,
            "will_delete_worktree": False,
            "will_mutate_github": False,
            "will_mutate_db_as_action": False,
            "will_start_background_worker": False,
        },
        "verifier_report": {
            "verifier_run_id": verifier_run_id,
            "verifier_report_path": str(verifier_report_path),
            "artifact_type": "scheduler_confirmation_verifier_report",
            "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
            "persisted": True,
            "status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
            "expiration": expiration,
        },
        "verifier_report_summary": {
            "schema_version": (
                "scheduler_confirmation_verifier_report.v1"
            ),
            "status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
            "failed_check_count": 0,
            "failed_check_names": [],
            "expiration": expiration,
        },
    }
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(handoff_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return handoff_path


# --------------------------------------------------------------------- main flow


def run_smoke(
    *,
    workspace_root: Path,
    base_branch: str = DEFAULT_BASE_BRANCH,
    issue_number: int = DEFAULT_ISSUE_NUMBER,
    repo: str = DEFAULT_REPO,
) -> dict[str, Any]:
    """Run the issue-to-waiting_approval smoke and return a JSON-safe summary."""

    paths = _prepare_chain_paths(workspace_root)
    base_sha = _init_git_repo(paths.repo_path, base_branch)
    paths.artifact_root.mkdir(parents=True, exist_ok=True)
    paths.worktree_root.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(paths.db_path)
    store.init_db()

    # 1. Deterministic Phase 6D issue intake (offline fetcher).
    issue_snapshot = _build_issue_snapshot(repo, issue_number)
    intake_result = intake_selected_github_issues(
        GitHubIssueIntakeRequest(
            repo=repo,
            issue_numbers=(issue_number,),
            repo_path=paths.repo_path,
            artifact_root=paths.artifact_root,
            project=DEFAULT_PROJECT,
            board=DEFAULT_BOARD,
            db_path=paths.db_path,
            dry_run=False,
        ),
        store=store,
        fetcher=lambda _repo, _number: issue_snapshot,
    )
    _require(intake_result["ok"], f"intake returned not-ok: {intake_result}")
    intake_selected = intake_result["selected"]
    _require(
        any(item["action"] == "ingested" for item in intake_selected),
        f"intake did not ingest the offline issue: {intake_selected}",
    )

    task_key = next(item["task_key"] for item in intake_selected if item["action"] == "ingested")
    task = store.get_task(task_key)
    _require(task is not None, "queued TaskRecord missing after intake")
    assert task is not None  # for type-checker
    _require(
        task.status == "queued",
        f"intake produced status={task.status!r}, expected 'queued'",
    )

    # 2. Phase 6E Task Execution Package (--confirm-create-package).
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
    artifact_dir = Path(package_result["artifact_dir"])
    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
    package_path = artifact_dir / PACKAGE_FILENAME
    _require(prompt_path.is_file(), f"implementation_prompt.md missing: {prompt_path}")
    _require(package_path.is_file(), f"task_execution_package.json missing: {package_path}")

    artifact_records = store.list_task_artifacts(task_key)
    artifact_types = {(record.artifact_type, str(record.path)) for record in artifact_records}
    _require(
        (PROMPT_ARTIFACT_TYPE, str(prompt_path)) in artifact_types,
        "implementation_prompt artifact record missing in store",
    )
    _require(
        (PACKAGE_ARTIFACT_TYPE, str(package_path)) in artifact_types,
        "task_execution_package artifact record missing in store",
    )

    events_after_package = store.list_task_events(task_key)
    package_events = [event for event in events_after_package if event.event_type == PACKAGE_EVENT_TYPE]
    _require(
        len(package_events) == 1,
        f"expected one task_execution_package_created event, got {len(package_events)}",
    )

    # 3. Synthesize a Phase A intake_runner_handoff binding for this
    #    queued task. The smoke does not exercise the full scheduler
    #    proposal/confirmation pipeline, so this helper writes the
    #    handoff artifact + persisted verifier report artifact pair on
    #    disk in the exact shape Phase A would have produced. Phase B's
    #    queued handoff preflight re-opens both artifacts and rejects
    #    the run if anything is malformed.
    handoff_artifact_path = _write_smoke_intake_runner_handoff_pair(
        artifact_root=paths.artifact_root,
        db_path=paths.db_path,
        task_key=task_key,
    )

    # 4. Phase 6E+1 explicit handoff to approved_task_runner, with injected
    #    fake executor and fake validator. preflight=False keeps the smoke
    #    hermetic (no real Pi/OpenCode/pytest check).
    handoff_result = run_queued_task_handoff(
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
            intake_runner_handoff_artifact_path=handoff_artifact_path,
        ),
        store=store,
        executor_registry={EXECUTOR_NAME: FakeLocalSmokeExecutor()},
        validator_registry={VALIDATOR_NAME: FakeLocalSmokeValidator()},
    )
    handoff_dict = handoff_result.to_dict()
    _require(
        handoff_dict["package"]["verified"] is True,
        "handoff did not verify the task execution package",
    )
    _require(
        handoff_dict["safety"]["approved_task_runner_started"] is True,
        "handoff did not call approved_task_runner",
    )
    _require(
        handoff_dict["ok"] is True,
        f"handoff was not ok: {handoff_dict.get('error')}",
    )
    _require(
        handoff_dict["status"] == APPROVED_TASK_STATUS,
        f"handoff status {handoff_dict['status']!r} != {APPROVED_TASK_STATUS!r}",
    )

    # 4. Store readback verifies waiting_approval and recorded evidence.
    final_task = store.get_task(task_key)
    _require(final_task is not None, "task missing after handoff")
    assert final_task is not None
    _require(
        final_task.status == APPROVED_TASK_STATUS,
        f"final task status {final_task.status!r} != {APPROVED_TASK_STATUS!r}",
    )

    executor_runs = store.list_executor_runs(task_key)
    validation_results = store.list_validation_results(task_key)
    final_artifacts = store.list_task_artifacts(task_key)

    _require(len(executor_runs) >= 1, f"no executor runs recorded: {executor_runs}")
    _require(
        any(run.get("status") == "completed" for run in executor_runs),
        "no executor run reached status=completed",
    )
    _require(len(validation_results) >= 1, f"no validation results recorded: {validation_results}")
    _require(
        any(result.get("status") == "passed" for result in validation_results),
        "no validation result reached status=passed",
    )

    runner_result = handoff_dict.get("runner_result") or {}

    return {
        "ok": True,
        "final_status": final_task.status,
        "task_key": task_key,
        "db_path": str(paths.db_path),
        "repo_path": str(paths.repo_path),
        "artifact_root": str(paths.artifact_root),
        "artifact_dir": str(artifact_dir),
        "worktree_root": str(paths.worktree_root),
        "issue_number": issue_number,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "package": {
            "implementation_prompt_path": str(prompt_path),
            "package_path": str(package_path),
            "package_event_count": len(package_events),
            "package_verified": True,
        },
        "handoff": {
            "ok": handoff_dict["ok"],
            "status": handoff_dict["status"],
            "phase": handoff_dict["phase"],
            "executor": handoff_dict["executor"],
            "approved_task_runner_started": handoff_dict["safety"]["approved_task_runner_started"],
            "validators_started": handoff_dict["safety"]["validators_started"],
            "executor_started": handoff_dict["safety"]["executor_started"],
            "workspace_prepared": handoff_dict["safety"]["workspace_prepared"],
        },
        "runner_summary": {
            "status": runner_result.get("status"),
            "phase": runner_result.get("phase"),
            "executor_started": (runner_result.get("safety") or {}).get("executor_started"),
            "validators_started": (runner_result.get("safety") or {}).get("validators_started"),
            "workspace_prepared": (runner_result.get("safety") or {}).get("workspace_prepared"),
        },
        "executor_run_count": len(executor_runs),
        "validation_result_count": len(validation_results),
        "artifact_count": len(final_artifacts),
        "safety": {
            "local_only": True,
            "used_real_executor": False,
            "network_used": False,
            "github_mutated": False,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "background_worker_started": False,
        },
    }


# --------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the local issue-to-waiting_approval smoke using offline "
            "fixtures and an injected fake executor/validator pair. "
            "No real Pi/OpenCode/gh/network."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root. If omitted, a temporary directory "
            "under $TMPDIR is created and cleaned up after the smoke unless "
            "--keep-workspace is supplied."
        ),
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=DEFAULT_ISSUE_NUMBER,
        help=f"Offline issue number. Default: {DEFAULT_ISSUE_NUMBER}.",
    )
    parser.add_argument(
        "--base-branch",
        default=DEFAULT_BASE_BRANCH,
        help=f"Base branch for the temp git repo. Default: {DEFAULT_BASE_BRANCH}.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help=(
            "Do not delete the temporary workspace after the smoke completes. "
            "Useful for operator inspection."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit compact JSON.",
    )
    parser.add_argument(
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

    compact = args.json and not args.pretty

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
            tempfile.mkdtemp(prefix="agent-taskflow-issue-to-waiting-approval-smoke-")
        )
        provided_workspace = False

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            base_branch=args.base_branch,
            issue_number=args.issue_number,
        )
    except SmokeFailure as exc:
        summary = {
            "ok": False,
            "error": str(exc),
            "workspace_root": str(workspace_root),
            "safety": _cli_error_safety(),
        }
        _emit(summary, compact=compact)
        if not args.keep_workspace and not provided_workspace:
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
        if not args.keep_workspace and not provided_workspace:
            _try_remove(workspace_root)
        return 1

    summary["workspace_root"] = str(workspace_root)
    summary["workspace_kept"] = bool(args.keep_workspace or provided_workspace)
    _emit(summary, compact=compact)

    if not args.keep_workspace and not provided_workspace:
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
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _try_remove(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:  # pragma: no cover - cleanup is best-effort
        pass


if __name__ == "__main__":
    raise SystemExit(main())
