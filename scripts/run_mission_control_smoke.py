#!/usr/bin/env python3
"""Run a deterministic Mission Control golden-path smoke.

This script exercises the backend path through the real Mission Control API
app, dispatcher, script-local executor, script-local validator, store, and API
readback endpoints. It does not call external workers or mutate the frontend.
"""

from __future__ import annotations

import argparse
import json
import shutil
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
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult


DEFAULT_TASK_KEY = "AT-MC-SMOKE"
DEFAULT_PROJECT = "agent-taskflow"
SMOKE_EXECUTOR = "smoke"
SMOKE_VALIDATOR = "smoke"
SMOKE_ARTIFACT_NAME = "mission_control_smoke_result.txt"
SMOKE_VALIDATOR_LOG_NAME = "mission-control-smoke-validator.log"
SMOKE_ARTIFACT_CONTENT = "mission-control-smoke-ok\n"


class SmokeFailure(RuntimeError):
    """Raised when the smoke path does not produce the expected result."""


class SmokeExecutor(Executor):
    """Script-local deterministic executor used only by this smoke."""

    name = SMOKE_EXECUTOR

    def __init__(self, artifact_content: str = SMOKE_ARTIFACT_CONTENT) -> None:
        self.artifact_content = artifact_content

    def run(self, context: ExecutorContext) -> ExecutorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = context.artifact_dir / SMOKE_ARTIFACT_NAME
        artifact_path.write_text(self.artifact_content, encoding="utf-8")
        return ExecutorResult(
            executor=self.name,
            status="completed",
            exit_code=0,
            summary="Smoke executor wrote deterministic artifact.",
            artifacts={"result": artifact_path},
        )


class SmokeValidator(Validator):
    """Script-local validator that verifies the smoke executor artifact."""

    name = SMOKE_VALIDATOR

    def __init__(self, expected_content: str = SMOKE_ARTIFACT_CONTENT) -> None:
        self.expected_content = expected_content

    def run(self, context: ValidatorContext) -> ValidatorResult:
        artifact_path = context.artifact_dir / SMOKE_ARTIFACT_NAME
        log_path = context.artifact_dir / SMOKE_VALIDATOR_LOG_NAME
        context.artifact_dir.mkdir(parents=True, exist_ok=True)

        if not artifact_path.is_file():
            summary = f"Smoke artifact missing: {artifact_path}"
            log_path.write_text(summary + "\n", encoding="utf-8")
            return ValidatorResult(
                validator=self.name,
                status="failed",
                exit_code=1,
                log_path=log_path,
                summary=summary,
                artifacts={"log": log_path},
            )

        actual = artifact_path.read_text(encoding="utf-8")
        if actual != self.expected_content:
            summary = "Smoke artifact content did not match expected deterministic content."
            log_path.write_text(
                f"{summary}\nexpected={self.expected_content!r}\nactual={actual!r}\n",
                encoding="utf-8",
            )
            return ValidatorResult(
                validator=self.name,
                status="failed",
                exit_code=1,
                log_path=log_path,
                summary=summary,
                artifacts={"log": log_path},
            )

        summary = "Smoke validator verified deterministic artifact."
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


def _assert_response(response: Any, expected_status: int, action: str) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise SmokeFailure(
            f"{action} returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{action} returned non-object JSON: {payload!r}")
    return payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _artifact_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in payload.get("items", []):
        if "name" in item:
            names.add(str(item["name"]))
        if "path" in item:
            names.add(Path(str(item["path"])).name)
    return names


def _make_dispatcher_factory(
    *,
    artifact_content: str,
    expected_content: str,
) -> Any:
    def dispatcher_factory(
        store: TaskMirrorStore,
        validators: Sequence[str],
    ) -> Dispatcher:
        return Dispatcher(
            store,
            executor_registry={
                SMOKE_EXECUTOR: SmokeExecutor(artifact_content=artifact_content),
            },
            validator_registry={
                SMOKE_VALIDATOR: SmokeValidator(expected_content=expected_content),
            },
            validators=validators,
            default_executor=SMOKE_EXECUTOR,
        )

    return dispatcher_factory


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
    artifact_content: str = SMOKE_ARTIFACT_CONTENT,
    expected_content: str = SMOKE_ARTIFACT_CONTENT,
) -> dict[str, Any]:
    """Run the smoke against an isolated workspace root and return a summary."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "mission-control-smoke.db"
    repo_path = workspace_root / "repo"
    worktree_path = repo_path / ".worktrees" / normalized_task_key
    artifact_dir = workspace_root / "artifacts" / normalized_task_key

    worktree_path.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(
        db_path=db_path,
        dispatcher_factory=_make_dispatcher_factory(
            artifact_content=artifact_content,
            expected_content=expected_content,
        ),
    )

    with TestClient(app) as client:
        health = _assert_response(client.get("/health"), 200, "health")
        _require(health.get("status") == "ok", "health endpoint did not return ok")

        create_payload = _assert_response(
            client.post(
                "/api/tasks",
                json={
                    "task_key": normalized_task_key,
                    "project": project,
                    "repo_path": str(repo_path),
                    "worktree_path": str(worktree_path),
                    "artifact_dir": str(artifact_dir),
                    "title": "Mission Control deterministic smoke",
                    "board": project,
                    "branch": f"smoke/{normalized_task_key}",
                    "base_branch": "main",
                },
            ),
            200,
            "create task",
        )
        _require(create_payload.get("ok") is True, "task create response was not ok")
        _require(create_payload.get("status") == "queued", "created task was not queued")

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

    expected_final_status = (
        "waiting_approval" if artifact_content == expected_content else "blocked"
    )
    _require(
        start_payload.get("status") == expected_final_status,
        f"start status mismatch: expected {expected_final_status}, got {start_payload.get('status')}",
    )
    _require(
        task_item.get("status") == expected_final_status,
        f"task readback status mismatch: expected {expected_final_status}, got {task_item.get('status')}",
    )
    _require(len(runs) == 1, f"expected one executor run, got {len(runs)}")
    _require(runs[0].get("executor") == SMOKE_EXECUTOR, "executor run name mismatch")
    _require(runs[0].get("status") == "completed", "executor run did not complete")
    _require(
        Path(str(runs[0].get("artifacts", {}).get("result", ""))).name
        == SMOKE_ARTIFACT_NAME,
        "executor artifact metadata did not include smoke result",
    )
    _require(len(validations) == 1, f"expected one validation result, got {len(validations)}")
    _require(
        validations[0].get("validator") == SMOKE_VALIDATOR,
        "validator result name mismatch",
    )

    expected_validator_status = "passed" if expected_final_status == "waiting_approval" else "failed"
    _require(
        validations[0].get("status") == expected_validator_status,
        f"validator status mismatch: expected {expected_validator_status}, got {validations[0].get('status')}",
    )
    _require(
        SMOKE_ARTIFACT_NAME in artifact_names,
        "artifact API did not list smoke result file",
    )
    _require(
        "mission_contract.json" in artifact_names,
        "artifact API did not list mission_contract.json",
    )
    _require(
        preview_payload.get("content") == artifact_content,
        "artifact preview content mismatch",
    )
    _require(
        evidence_item.get("mission_contract", {}).get("executor") == SMOKE_EXECUTOR,
        "review evidence did not read mission contract executor",
    )

    return {
        "ok": expected_final_status == "waiting_approval",
        "task_key": normalized_task_key,
        "final_status": task_item.get("status"),
        "db_path": str(db_path),
        "workspace_root": str(workspace_root),
        "repo_path": str(repo_path),
        "worktree_path": str(worktree_path),
        "artifact_dir": str(artifact_dir),
        "executor": {
            "name": runs[0].get("executor"),
            "status": runs[0].get("status"),
            "artifact": str(artifact_dir / SMOKE_ARTIFACT_NAME),
        },
        "validator": {
            "name": validations[0].get("validator"),
            "status": validations[0].get("status"),
            "log": validations[0].get("log_path"),
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
        description="Run the deterministic Mission Control backend smoke.",
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
            "under /tmp is created and removed after the run."
        ),
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the auto-created temporary workspace after the run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cleanup_workspace = False
    if args.workspace_root:
        workspace_root = _require_absolute_path(args.workspace_root, "workspace_root")
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="agent-taskflow-mc-smoke-"))
        cleanup_workspace = not args.keep_workspace

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
        )
        summary["workspace_kept"] = not cleanup_workspace
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Mission Control smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_workspace:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
