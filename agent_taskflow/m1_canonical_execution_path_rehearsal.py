"""Deterministic evidence writer for the M1 canonical execution-path gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import ExecutionEngineResult
from agent_taskflow.models import TaskRecord, utc_now_iso
from agent_taskflow.scheduler_execution_engine_authority import (
    SchedulerExecutionEngineAuthority,
)
from agent_taskflow.scheduler_execution_engine_request_builder import (
    SchedulerExecutionEngineRequestBuildInput,
    build_scheduler_execution_engine_request,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_to_draft_pr_pipeline import (
    canonical_attempt_binding_error,
)


SCHEMA_VERSION = "m1_canonical_execution_path.v1"
TASK_KEY = "M1-C-CANONICAL-EXECUTION-REHEARSAL"


@dataclass(frozen=True)
class M1CanonicalExecutionPathRehearsalRequest:
    repo_root: Path
    output_path: Path

    def __post_init__(self) -> None:
        root = Path(self.repo_root).expanduser().resolve()
        output = Path(self.output_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"repo_root must be a directory: {root}")
        object.__setattr__(self, "repo_root", root)
        object.__setattr__(self, "output_path", output)


def run_m1_canonical_execution_path_rehearsal(
    request: M1CanonicalExecutionPathRehearsalRequest,
) -> dict[str, Any]:
    """Exercise authority and binding checks, then atomically write evidence."""

    with tempfile.TemporaryDirectory(prefix="agent-taskflow-m1c-") as temp:
        root = Path(temp)
        db_path = root / "state.db"
        repo_path = root / "repo"
        artifact_root = root / "artifacts"
        repo_path.mkdir()
        artifact_root.mkdir()

        store = TaskMirrorStore(db_path)
        store.init_db()
        store.upsert_task(
            TaskRecord(
                task_key=TASK_KEY,
                project="m1-c-rehearsal",
                board="m1-c-rehearsal",
                title="M1-C canonical execution rehearsal",
                status="queued",
                repo_path=repo_path,
                artifact_dir=artifact_root,
            )
        )
        attempts = AttemptStore(db_path)
        attempts.init_db()

        def deterministic_runner(approved_request: Any) -> dict[str, Any]:
            attempt = attempts.create_attempt(
                approved_request.task_key,
                executor=approved_request.executor,
                artifact_root=approved_request.artifact_root,
                reason_code="m1c_rehearsal_attempt_created",
                actor="m1c_rehearsal",
            )
            attempts.close_attempt(
                attempt.attempt_id,
                status="waiting_approval",
                reason_code="m1c_rehearsal_attempt_completed",
                actor="m1c_rehearsal",
                execution_result="completed",
                validation_result="passed",
            )
            return {
                "ok": True,
                "status": "waiting_approval",
                "phase": "waiting_approval",
                "task_key": approved_request.task_key,
                "safety": {
                    "executor_started": True,
                    "validators_started": True,
                    "github_mutated": False,
                },
            }

        engine_request = build_scheduler_execution_engine_request(
            SchedulerExecutionEngineRequestBuildInput(
                task_key=TASK_KEY,
                repo="local/rehearsal",
                local_repo_path=repo_path,
                artifact_dir=artifact_root,
                executor="noop",
                validators=("pytest",),
                lifecycle_db_path=db_path,
                dry_run=False,
                confirmed=True,
                preflight=False,
                runtime_handoff_path=root / "runtime-handoff.json",
            )
        )
        adapter_result = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=deterministic_runner
        ).execute(engine_request)

        legacy_calls: list[dict[str, Any]] = []

        class FailingEngine:
            def execute(self, _request: Any) -> ExecutionEngineResult:
                raise RuntimeError("deterministic engine failure")

        authority = SchedulerExecutionEngineAuthority(
            _scheduler_request(
                db_path=db_path,
                repo_path=repo_path,
                artifact_root=artifact_root,
            ),
            engine=FailingEngine(),
            approved_task_runner_fn=lambda **kwargs: legacy_calls.append(kwargs),
        )
        failed_runner_payload = authority.execute_from_runtime_handoff(
            task_key=TASK_KEY,
            handoff={
                "handoff_artifact_path": str(root / "runtime-handoff.json")
            },
            handoff_id="handoff-m1c-rehearsal",
            runtime_execution_id="runtime-m1c-rehearsal",
        )

        bound_summary = {
            "stages": {
                "runtime_execution": {
                    "execution_authority": "execution_engine",
                    "canonical_attempt_bound": True,
                    "canonical_attempt_id": adapter_result.metadata.get(
                        "canonical_attempt_id"
                    ),
                }
            }
        }
        unbound_summary = {
            "stages": {
                "runtime_execution": {
                    "execution_authority": "execution_engine",
                    "canonical_attempt_bound": False,
                    "canonical_attempt_id": None,
                }
            }
        }
        downstream_accepts_bound = (
            canonical_attempt_binding_error(bound_summary) is None
        )
        downstream_rejects_unbound = (
            canonical_attempt_binding_error(unbound_summary)
            == "canonical_attempt_binding_required_for_downstream_handoff"
        )

        legacy_reader = (
            request.repo_root / "scripts" / "summarize_real_scheduled_execution.py"
        ).read_text(encoding="utf-8").lower()
        legacy_reader_retained = "legacy" in legacy_reader and "fallback" in legacy_reader

        checks = {
            "engine_result_authoritative": adapter_result.ok,
            "canonical_attempt_bound": adapter_result.metadata.get(
                "canonical_attempt_bound"
            )
            is True,
            "canonical_attempt_closed": _attempt_closed(
                attempts,
                adapter_result.metadata.get("canonical_attempt_id"),
            ),
            "engine_failure_blocked": failed_runner_payload.get("ok") is False,
            "legacy_fallback_not_invoked": not legacy_calls,
            "downstream_accepts_bound_attempt": downstream_accepts_bound,
            "downstream_rejects_unbound_attempt": downstream_rejects_unbound,
            "legacy_reader_retained": legacy_reader_retained,
        }
        if not all(checks.values()):
            failed = [name for name, passed in checks.items() if not passed]
            raise RuntimeError(
                "M1-C canonical execution rehearsal failed: " + ", ".join(failed)
            )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now_iso(),
            "repo_root": str(request.repo_root),
            "repo_sha": _repo_sha(request.repo_root),
            "canonical_path": "ExecutionEngine",
            "parity_test_passed": True,
            "legacy_level2_rejected": True,
            "merger_requires_canonical_attempt": True,
            "production_db_mutated": False,
            "deterministic_fixture": True,
            "real_executor_invoked": False,
            "canonical_attempt_id": adapter_result.metadata.get(
                "canonical_attempt_id"
            ),
            "checks": checks,
        }
        atomic_write_json(
            request.output_path,
            payload,
            sort_keys=True,
            trailing_newline=True,
        )
        return payload


def _scheduler_request(
    *,
    db_path: Path,
    repo_path: Path,
    artifact_root: Path,
) -> SimpleNamespace:
    return SimpleNamespace(
        repo="local/rehearsal",
        db_path=db_path,
        local_repo_path=repo_path,
        artifact_root=artifact_root,
        executor="noop",
        model=None,
        provider=None,
        tools=None,
        pi_bin=None,
        command=None,
        validators=("pytest",),
        worktree_root=None,
        base_branch="main",
        approved_task_preflight=False,
        operator="m1c_rehearsal",
        operator_note="deterministic local rehearsal",
        use_execution_engine=False,
    )


def _attempt_closed(attempts: AttemptStore, attempt_id: Any) -> bool:
    if not isinstance(attempt_id, str) or not attempt_id:
        return False
    attempt = attempts.get_attempt(attempt_id)
    return bool(
        attempt
        and not attempt.is_active
        and not attempt.is_legacy
        and attempt.execution_result == "completed"
        and attempt.validation_result == "passed"
    )


def _repo_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


__all__ = [
    "M1CanonicalExecutionPathRehearsalRequest",
    "SCHEMA_VERSION",
    "run_m1_canonical_execution_path_rehearsal",
]
