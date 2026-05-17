#!/usr/bin/env python3
"""Run the prepared-workspace golden-path smoke.

This smoke is local-only. It creates a temporary git repository and Agent
Taskflow DB, prepares a task workspace through the API, dispatches the task
with script-local executor and validator implementations, and verifies review
evidence readback. It does not call external workers or GitHub.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult


DEFAULT_TASK_KEY = "AT-PREPARED-WORKSPACE-SMOKE"
DEFAULT_PROJECT = "agent-taskflow"
SMOKE_EXECUTOR = "prepared-workspace-smoke"
SMOKE_VALIDATOR = "prepared-workspace-smoke"
SMOKE_ARTIFACT_NAME = "prepared_workspace_smoke_result.txt"
SMOKE_WORKTREE_FILE = "prepared_workspace_smoke.txt"
SMOKE_VALIDATOR_LOG_NAME = "prepared-workspace-smoke-validator.log"
SMOKE_ARTIFACT_CONTENT = "prepared-workspace-smoke-ok\n"
SMOKE_WORKTREE_CONTENT = "prepared workspace executor touched this worktree\n"


class SmokeFailure(RuntimeError):
    """Raised when the prepared-workspace smoke does not meet expectations."""


class PreparedWorkspaceSmokeExecutor(Executor):
    """Script-local executor proving dispatcher uses the prepared worktree."""

    name = SMOKE_EXECUTOR

    def run(self, context: ExecutorContext) -> ExecutorResult:
        if not context.worktree_path.is_dir():
            return ExecutorResult(
                executor=self.name,
                status="blocked",
                exit_code=1,
                summary=f"Prepared worktree does not exist: {context.worktree_path}",
            )

        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = context.artifact_dir / SMOKE_ARTIFACT_NAME
        worktree_marker = context.worktree_path / SMOKE_WORKTREE_FILE

        artifact_path.write_text(SMOKE_ARTIFACT_CONTENT, encoding="utf-8")
        worktree_marker.write_text(SMOKE_WORKTREE_CONTENT, encoding="utf-8")

        return ExecutorResult(
            executor=self.name,
            status="completed",
            exit_code=0,
            summary="Prepared workspace smoke executor wrote artifact and worktree marker.",
            artifacts={
                "result": artifact_path,
                "worktree_marker": worktree_marker,
            },
        )


class PreparedWorkspaceSmokeValidator(Validator):
    """Script-local validator for prepared workspace proof-of-work."""

    name = SMOKE_VALIDATOR

    def run(self, context: ValidatorContext) -> ValidatorResult:
        artifact_path = context.artifact_dir / SMOKE_ARTIFACT_NAME
        worktree_marker = context.worktree_path / SMOKE_WORKTREE_FILE
        contract_path = context.artifact_dir / "mission_contract.json"
        log_path = context.artifact_dir / SMOKE_VALIDATOR_LOG_NAME
        context.artifact_dir.mkdir(parents=True, exist_ok=True)

        failures: list[str] = []
        if not context.worktree_path.is_dir():
            failures.append(f"prepared worktree missing: {context.worktree_path}")
        if not artifact_path.is_file():
            failures.append(f"executor artifact missing: {artifact_path}")
        elif artifact_path.read_text(encoding="utf-8") != SMOKE_ARTIFACT_CONTENT:
            failures.append("executor artifact content mismatch")
        if not worktree_marker.is_file():
            failures.append(f"worktree marker missing: {worktree_marker}")
        elif worktree_marker.read_text(encoding="utf-8") != SMOKE_WORKTREE_CONTENT:
            failures.append("worktree marker content mismatch")
        if not contract_path.is_file():
            failures.append(f"mission_contract.json missing: {contract_path}")

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

        summary = "Prepared workspace smoke validator verified artifact, worktree, and contract."
        log_path.write_text(summary + "\n", encoding="utf-8")
        return ValidatorResult(
            validator=self.name,
            status="passed",
            exit_code=0,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path},
        )


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _assert_response(response: Any, expected_status: int, action: str) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise SmokeFailure(
            f"{action} returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{action} returned non-object JSON: {payload!r}")
    return payload


def _artifact_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in payload.get("items", []):
        if "name" in item:
            names.add(str(item["name"]))
        if "path" in item:
            names.add(Path(str(item["path"])).name)
    return names


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
            f"git {' '.join(args)} failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _init_git_repo(repo_path: Path) -> str:
    repo_path.mkdir(parents=True, exist_ok=True)
    _run_git(repo_path, ["init"])
    _run_git(repo_path, ["config", "user.email", "agent-taskflow@example.invalid"])
    _run_git(repo_path, ["config", "user.name", "Agent Taskflow Smoke"])
    (repo_path / "README.md").write_text("# prepared workspace smoke\n", encoding="utf-8")
    _run_git(repo_path, ["add", "README.md"])
    _run_git(repo_path, ["commit", "-m", "initial"])
    _run_git(repo_path, ["branch", "-M", "main"])
    return _run_git(repo_path, ["rev-parse", "main"])


def _make_dispatcher_factory() -> Any:
    def dispatcher_factory(
        store: TaskMirrorStore,
        validators: Sequence[str],
    ) -> Dispatcher:
        return Dispatcher(
            store,
            executor_registry={
                SMOKE_EXECUTOR: PreparedWorkspaceSmokeExecutor(),
            },
            validator_registry={
                SMOKE_VALIDATOR: PreparedWorkspaceSmokeValidator(),
            },
            validators=validators,
            default_executor=SMOKE_EXECUTOR,
        )

    return dispatcher_factory


def _create_task_record(
    store: TaskMirrorStore,
    *,
    task_key: str,
    project: str,
    repo_path: Path,
    artifact_dir: Path,
) -> None:
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=project,
            board=project,
            title="Prepared workspace golden-path smoke",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
        )
    )
    store.record_task_event(
        task_key,
        "created",
        "prepared_workspace_smoke",
        message="Task created by prepared workspace smoke",
        payload={"kind": "task_created"},
    )


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
    skip_prepare_for_test: bool = False,
) -> dict[str, Any]:
    """Run the prepared-workspace smoke and return a machine-readable summary."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "prepared-workspace-smoke.db"
    repo_path = workspace_root / "repo"
    artifact_dir = workspace_root / "artifacts" / normalized_task_key

    base_sha = _init_git_repo(repo_path)
    store = TaskMirrorStore(db_path)
    store.init_db()
    _create_task_record(
        store,
        task_key=normalized_task_key,
        project=project,
        repo_path=repo_path,
        artifact_dir=artifact_dir,
    )
    _require(
        store.get_task_worktree(normalized_task_key) is None,
        "smoke unexpectedly pre-created a worktree record",
    )

    app = create_app(
        db_path=db_path,
        dispatcher_factory=_make_dispatcher_factory(),
    )

    prepare_payload: dict[str, Any] | None = None
    prepare_status = "skipped"
    prepare_verified_before_dispatch = False

    with TestClient(app) as client:
        health = _assert_response(client.get("/health"), 200, "health")
        _require(health.get("status") == "ok", "health endpoint did not return ok")

        task_before_prepare = _assert_response(
            client.get(f"/api/tasks/{normalized_task_key}"),
            200,
            "task readback before prepare",
        )
        _require(
            task_before_prepare.get("item", {}).get("status") == "queued",
            "task was not queued before prepare",
        )

        if not skip_prepare_for_test:
            prepare_payload = _assert_response(
                client.post(
                    f"/api/tasks/{normalized_task_key}/prepare-workspace",
                    json={"base_branch": "main"},
                ),
                200,
                "prepare workspace",
            )
            _require(prepare_payload.get("ok") is True, "prepare workspace response was not ok")
            prepare_status = str(prepare_payload.get("status"))

        prepared_record = store.get_task_worktree(normalized_task_key)
        if prepared_record is None or not prepared_record.base_sha:
            raise SmokeFailure("prepare workspace must record base_sha before dispatch")

        _require(
            prepared_record.status == "active",
            f"prepared worktree status mismatch: {prepared_record.status}",
        )
        _require(
            prepared_record.base_branch == "main",
            f"prepared base_branch mismatch: {prepared_record.base_branch}",
        )
        _require(
            prepared_record.base_sha == base_sha,
            f"prepared base_sha mismatch: expected {base_sha}, got {prepared_record.base_sha}",
        )
        _require(
            prepared_record.worktree_path.is_dir(),
            f"prepared worktree path is missing: {prepared_record.worktree_path}",
        )
        prepare_verified_before_dispatch = True

        start_payload = _assert_response(
            client.post(
                f"/api/tasks/{normalized_task_key}/start",
                json={
                    "executor": SMOKE_EXECUTOR,
                    "validators": [SMOKE_VALIDATOR],
                },
            ),
            200,
            "start task",
        )

        task_payload = _assert_response(
            client.get(f"/api/tasks/{normalized_task_key}"),
            200,
            "task readback",
        )
        runs_payload = _assert_response(
            client.get(f"/api/tasks/{normalized_task_key}/runs"),
            200,
            "runs readback",
        )
        validations_payload = _assert_response(
            client.get(f"/api/tasks/{normalized_task_key}/validations"),
            200,
            "validations readback",
        )
        artifacts_payload = _assert_response(
            client.get(f"/api/tasks/{normalized_task_key}/artifacts"),
            200,
            "artifacts readback",
        )
        preview_payload = _assert_response(
            client.get(
                f"/api/tasks/{normalized_task_key}/artifacts/{SMOKE_ARTIFACT_NAME}"
            ),
            200,
            "artifact preview readback",
        )
        evidence_payload = _assert_response(
            client.get(f"/api/tasks/{normalized_task_key}/review-evidence"),
            200,
            "review evidence readback",
        )

    task_item = task_payload.get("item", {})
    runs = runs_payload.get("items", [])
    validations = validations_payload.get("items", [])
    artifact_names = _artifact_names(artifacts_payload)
    evidence_item = evidence_payload.get("item", {})
    dispatcher_status = str(start_payload.get("status"))

    _require(dispatcher_status == "waiting_approval", f"dispatcher status mismatch: {dispatcher_status}")
    _require(task_item.get("status") == "waiting_approval", "task did not reach waiting_approval")
    _require(len(runs) == 1, f"expected one executor run, got {len(runs)}")
    _require(runs[0].get("executor") == SMOKE_EXECUTOR, "executor run name mismatch")
    _require(runs[0].get("status") == "completed", "executor run did not complete")
    _require(len(validations) == 1, f"expected one validation result, got {len(validations)}")
    _require(validations[0].get("validator") == SMOKE_VALIDATOR, "validator name mismatch")
    _require(validations[0].get("status") == "passed", "validator did not pass")
    _require("mission_contract.json" in artifact_names, "mission_contract.json not listed")
    _require(SMOKE_ARTIFACT_NAME in artifact_names, "executor artifact not listed")
    _require(
        preview_payload.get("content") == SMOKE_ARTIFACT_CONTENT,
        "artifact preview content mismatch",
    )
    _require(
        evidence_item.get("mission_contract", {}).get("executor") == SMOKE_EXECUTOR,
        "review evidence did not read mission contract executor",
    )

    review_evidence_available = bool(
        evidence_item.get("mission_contract")
        and evidence_item.get("validator_results")
        and evidence_item.get("artifacts")
    )
    _require(review_evidence_available, "review evidence was incomplete")

    return {
        "ok": True,
        "db_path": str(db_path),
        "repo_path": str(repo_path),
        "task_key": normalized_task_key,
        "worktree_path": str(prepared_record.worktree_path),
        "branch": prepared_record.branch,
        "base_branch": prepared_record.base_branch,
        "base_sha": prepared_record.base_sha,
        "artifact_dir": str(artifact_dir),
        "final_status": task_item.get("status"),
        "prepare_status": prepare_status,
        "dispatcher_status": dispatcher_status,
        "prepare_verified_before_dispatch": prepare_verified_before_dispatch,
        "review_evidence_available": review_evidence_available,
        "validation_summary": {
            "validator": validations[0].get("validator"),
            "status": validations[0].get("status"),
            "summary": validations[0].get("summary"),
            "log_path": validations[0].get("log_path"),
        },
        "executor_summary": {
            "executor": runs[0].get("executor"),
            "status": runs[0].get("status"),
            "summary": runs[0].get("summary"),
            "artifacts": runs[0].get("artifacts", {}),
        },
        "readbacks": {
            "task": task_item.get("status"),
            "runs": runs_payload.get("count"),
            "validations": validations_payload.get("count"),
            "artifacts": sorted(artifact_names),
            "review_evidence_contract": evidence_item.get("mission_contract", {}).get("status"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local prepared-workspace golden-path smoke.",
    )
    parser.add_argument(
        "--task-key",
        default=DEFAULT_TASK_KEY,
        help=f"Task key to use. Default: {DEFAULT_TASK_KEY}",
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root to use. By default a temporary directory "
            "under /tmp is created and preserved for proof-of-work inspection."
        ),
    )
    parser.add_argument(
        "--skip-prepare-for-test",
        action="store_true",
        help="Testing-only failure path: skip explicit prepare before dispatch.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.workspace_root:
        workspace_root = _require_absolute_path(args.workspace_root, "workspace_root")
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="agent-taskflow-prepared-smoke-"))

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
            skip_prepare_for_test=args.skip_prepare_for_test,
        )
        summary["workspace_kept"] = True
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Prepared workspace golden-path smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
