"""Deterministic repository-wide evidence writer for the M1-C exit gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Callable

from agent_taskflow.approved_task_runner import (
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import ExecutionEngineResult
from agent_taskflow.execution_engine_manual_runtime import (
    build_manual_execution_engine_request,
)
from agent_taskflow.level2_execution_authority import (
    Level2ExecutionAuthorityError,
    verify_canonical_attempt,
)
from agent_taskflow.models import TaskRecord, utc_now_iso
from agent_taskflow.one_shot_task_pipeline import (
    OneShotTaskPipelineRequest,
    run_one_shot_task_pipeline,
)
from agent_taskflow.pr_handoff import _canonical_attempt_binding
from agent_taskflow.queued_task_handoff import (
    QueuedTaskHandoffRequest,
    run_queued_task_handoff,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RuntimeHandoffExecutionRequest,
    run_runtime_handoff_execution_from_handoff,
)
from agent_taskflow.scheduler_execution_engine_authority import (
    SchedulerExecutionEngineAuthority,
)
from agent_taskflow.scheduler_execution_engine_opt_in import (
    route_scheduler_tick_through_execution_engine,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_to_draft_pr_pipeline import (
    canonical_attempt_binding_error,
)


SCHEMA_VERSION = "m1_canonical_execution_path.v2"
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


class _NeverCalled:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        raise AssertionError("legacy Level 2 callback must not run")

    def run(self, context: Any) -> Any:
        self.calls.append(context)
        raise AssertionError("legacy Level 2 executor must not run")

    def execute(self, request: Any) -> Any:
        self.calls.append(request)
        raise AssertionError("historical shadow engine must not run")


def run_m1_canonical_execution_path_rehearsal(
    request: M1CanonicalExecutionPathRehearsalRequest,
) -> dict[str, Any]:
    """Exercise every repository-wide authority semantic and write evidence."""

    with tempfile.TemporaryDirectory(prefix="agent-taskflow-m1c-") as temp:
        root = Path(temp)
        db_path = root / "state.db"
        repo_path = root / "repo"
        artifact_root = root / "artifacts"
        repo_path.mkdir()
        artifact_root.mkdir()
        _init_git_repo(repo_path)

        store = TaskMirrorStore(db_path)
        store.init_db()
        attempts = AttemptStore(db_path)
        attempts.init_db()
        _add_task(store, attempts, TASK_KEY, repo_path, artifact_root, level2=True)

        def deterministic_runner(
            approved_request: Any,
            *,
            store: Any = None,
        ) -> dict[str, Any]:
            # The engine supplies the canonical runtime store. Claiming through
            # it reserves the Attempt before execution and closes it on release,
            # which is the binding the authority then verifies.
            store.update_task_status(
                approved_request.task_key,
                "preparing",
                source="m1c_rehearsal_execution_engine",
                message="Authoritative rehearsal execution claimed the task",
            )
            store.update_task_status(
                approved_request.task_key,
                "waiting_approval",
                source="m1c_rehearsal_execution_engine",
                message="Authoritative rehearsal execution completed",
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

        authority = SchedulerExecutionEngineAuthority(
            _scheduler_request(
                db_path=db_path,
                repo_path=repo_path,
                artifact_root=artifact_root,
            ),
            engine=ApprovedTaskRunnerExecutionEngineAdapter(
                approved_task_runner=deterministic_runner
            ),
        )
        authoritative_pipeline = run_one_shot_task_pipeline(
            OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key=TASK_KEY,
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
                operator="m1c_rehearsal",
                operator_note="repository-wide authority rehearsal",
            ),
            approved_task_runner_fn=authority.execute_from_runtime_handoff,
        )
        authoritative_runtime = authoritative_pipeline.get("stages", {}).get(
            "runtime_execution", {}
        )
        authoritative_attempt_id = authoritative_runtime.get(
            "canonical_attempt_id"
        )
        authority_evidence = authority.evidence(authoritative_pipeline)

        direct_script = _run_direct_legacy_script(
            request.repo_root,
            db_path=db_path,
            repo_path=repo_path,
            artifact_root=artifact_root,
        )

        queued_runner = _NeverCalled()
        queued_result = run_queued_task_handoff(
            QueuedTaskHandoffRequest(
                task_key=TASK_KEY,
                executor="manual",
                repo_path=repo_path,
                db_path=db_path,
                artifact_root=artifact_root,
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=root / "queued-handoff.json",
            ),
            store=store,
            approved_task_runner=queued_runner,
        )
        dispatcher_executor = _NeverCalled()
        dispatcher_result = Dispatcher(
            store,
            executor_registry={"manual": dispatcher_executor},
            validators=(),
            default_executor="manual",
        ).dispatch_task(TASK_KEY)
        manual_facade_runner = _NeverCalled()
        manual_facade_request = replace(
            build_manual_execution_engine_request(
                task_key=TASK_KEY,
                repo_path=repo_path,
                artifact_dir=artifact_root,
                dry_run=False,
            ),
            lifecycle_db_path=db_path,
        )
        manual_facade_result = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=manual_facade_runner
        ).execute(manual_facade_request)
        historical_shadow_engine = _NeverCalled()
        historical_shadow = route_scheduler_tick_through_execution_engine(
            SimpleNamespace(db_path=db_path),
            {
                "ok": True,
                "status": "execution_completed",
                "mode": "confirmed",
                "selected_task_key": TASK_KEY,
                "safety": {},
            },
            engine=historical_shadow_engine,
        )

        injected_runtime_runner = _NeverCalled()
        injected_runtime = run_runtime_handoff_execution_from_handoff(
            RuntimeHandoffExecutionRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key=TASK_KEY,
                handoff_id="injected-runner-handoff",
                dry_run=False,
                confirm_run_approved_task_runner=True,
            ),
            approved_task_runner_fn=injected_runtime_runner,
        )
        injected_one_shot_runner = _NeverCalled()
        injected_one_shot = run_one_shot_task_pipeline(
            OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key=TASK_KEY,
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
            ),
            approved_task_runner_fn=injected_one_shot_runner,
        )

        fake_attempt_checks = _exercise_fake_attempt_claims(
            store,
            attempts,
            repo_path,
            artifact_root,
        )

        attempt_b = attempts.create_attempt(TASK_KEY)
        attempts.close_attempt(
            attempt_b.attempt_id,
            status="waiting_approval",
            reason_code="newer_attempt_b_completed",
            actor="m1c_rehearsal",
            execution_result="completed",
            validation_result="passed",
        )
        downstream_exact = canonical_attempt_binding_error(
            authoritative_pipeline,
            db_path=db_path,
            task_key=TASK_KEY,
        ) is None
        pr_binding = _canonical_attempt_binding(
            db_path,
            TASK_KEY,
            canonical_attempt_id=str(authoritative_attempt_id),
        )
        pr_exact = bool(
            pr_binding
            and pr_binding.get("attempt_id") == authoritative_attempt_id
            and pr_binding.get("attempt_id") != attempt_b.attempt_id
            and pr_binding.get("store_verified") is True
        )

        legacy_calls = _NeverCalled()

        class FailingEngine:
            def execute(self, _request: Any) -> ExecutionEngineResult:
                raise RuntimeError("deterministic engine failure")

        failing_authority = SchedulerExecutionEngineAuthority(
            _scheduler_request(
                db_path=db_path,
                repo_path=repo_path,
                artifact_root=artifact_root,
            ),
            engine=FailingEngine(),
            approved_task_runner_fn=legacy_calls,
        )
        failed_payload = failing_authority.execute_from_runtime_handoff(
            task_key=TASK_KEY,
            handoff={"handoff_artifact_path": str(root / "failure-handoff.json")},
            handoff_id="handoff-failure",
            runtime_execution_id="runtime-failure",
        )

        legacy_task_key = "M1-C-LEGACY-COMPATIBILITY"
        _add_task(
            store,
            attempts,
            legacy_task_key,
            repo_path,
            artifact_root,
            level2=False,
        )
        legacy_preview = run_approved_task(
            ApprovedTaskRunRequest(
                task_key=legacy_task_key,
                executor="shell",
                command=("/bin/true",),
                repo_path=repo_path,
                db_path=db_path,
                artifact_root=artifact_root,
                dry_run=True,
                preflight=False,
            )
        )
        legacy_reader = (
            request.repo_root / "scripts" / "summarize_real_scheduled_execution.py"
        ).read_text(encoding="utf-8").lower()

        checks = {
            "scheduler_level2_engine_authoritative": bool(
                authoritative_pipeline.get("ok")
                and authority.invocation_count == 1
                and authority_evidence.get(
                    "canonical_attempt_store_verified"
                )
                is True
            ),
            "direct_legacy_level2_entry_blocked": bool(
                direct_script.get("returncode") == 1
                and direct_script.get("payload", {}).get("phase")
                == "execution_authority"
                and direct_script.get("payload", {}).get("safety", {}).get(
                    "executor_started"
                )
                is False
            ),
            "alternate_level2_entrypoints_engine_or_fail_closed": bool(
                not queued_result.ok
                and queued_result.phase == "execution_authority"
                and not queued_runner.calls
                and dispatcher_result.status == "blocked"
                and not dispatcher_executor.calls
                and not manual_facade_result.ok
                and not manual_facade_runner.calls
                and historical_shadow.get("executed") is False
                and not historical_shadow_engine.calls
            ),
            "injected_runner_level2_bypass_blocked": bool(
                injected_runtime.get("status") == "execution_authority_blocked"
                and not injected_runtime_runner.calls
                and injected_one_shot.get("ok") is False
                and "level2_execution_engine_authority_required"
                in (injected_one_shot.get("reasons") or [])
                and not injected_one_shot_runner.calls
            ),
            "engine_canonical_attempt_verified_in_store": bool(
                authoritative_attempt_id
                and authority_evidence.get(
                    "canonical_attempt_store_verified"
                )
                is True
                and all(fake_attempt_checks.values())
            ),
            "downstream_exact_attempt_binding_verified": downstream_exact,
            "pr_handoff_exact_attempt_binding_verified": pr_exact,
            "engine_failure_legacy_fallback_blocked": bool(
                failed_payload.get("ok") is False and not legacy_calls.calls
            ),
            "legacy_reader_compatibility_retained": bool(
                legacy_preview.ok
                and legacy_preview.status == "preview"
                and "legacy" in legacy_reader
                and "fallback" in legacy_reader
            ),
        }
        if not all(checks.values()):
            failed = [name for name, passed in checks.items() if not passed]
            raise RuntimeError(
                "M1-C repository-wide rehearsal failed: " + ", ".join(failed)
            )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now_iso(),
            "repo_root": str(request.repo_root),
            "repo_sha": _repo_sha(request.repo_root),
            "canonical_path": "ExecutionEngine",
            **checks,
            "production_db_mutated": False,
            "deterministic_fixture": True,
            "real_executor_invoked": False,
            "canonical_attempt_id": authoritative_attempt_id,
            "adversarial_attempt_checks": fake_attempt_checks,
            "checks": checks,
        }
        atomic_write_json(
            request.output_path,
            payload,
            sort_keys=True,
            trailing_newline=True,
        )
        return payload


def _add_task(
    store: TaskMirrorStore,
    attempts: AttemptStore,
    task_key: str,
    repo_path: Path,
    artifact_root: Path,
    *,
    level2: bool,
) -> None:
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project="m1-c-rehearsal",
            board="m1-c-rehearsal",
            title=task_key,
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_root / task_key,
        )
    )
    if level2:
        attempts.register_task_identity(
            task_key,
            task_class="canonical",
            is_legacy=False,
        )


def _exercise_fake_attempt_claims(
    store: TaskMirrorStore,
    attempts: AttemptStore,
    repo_path: Path,
    artifact_root: Path,
) -> dict[str, bool]:
    wrong_key = "M1-C-WRONG-TASK"
    active_key = "M1-C-ACTIVE-ATTEMPT"
    _add_task(store, attempts, wrong_key, repo_path, artifact_root, level2=True)
    _add_task(store, attempts, active_key, repo_path, artifact_root, level2=True)
    wrong = attempts.create_attempt(wrong_key)
    attempts.close_attempt(
        wrong.attempt_id,
        status="waiting_approval",
        reason_code="wrong_task_attempt_complete",
        actor="m1c_rehearsal",
        execution_result="completed",
        validation_result="passed",
    )
    active = attempts.create_attempt(active_key)
    return {
        "nonexistent_attempt_rejected": _verification_rejected(
            lambda: verify_canonical_attempt(
                db_path=attempts.db_path,
                task_key=TASK_KEY,
                attempt_id="attempt-does-not-exist",
            )
        ),
        "wrong_task_attempt_rejected": _verification_rejected(
            lambda: verify_canonical_attempt(
                db_path=attempts.db_path,
                task_key=TASK_KEY,
                attempt_id=wrong.attempt_id,
            )
        ),
        "nonterminal_attempt_rejected": _verification_rejected(
            lambda: verify_canonical_attempt(
                db_path=attempts.db_path,
                task_key=active_key,
                attempt_id=active.attempt_id,
            )
        ),
    }


def _verification_rejected(callback: Callable[[], Any]) -> bool:
    try:
        callback()
    except Level2ExecutionAuthorityError:
        return True
    return False


def _run_direct_legacy_script(
    repo_root: Path,
    *,
    db_path: Path,
    repo_path: Path,
    artifact_root: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_approved_task.py"),
            "--task-key",
            TASK_KEY,
            "--executor",
            "manual",
            "--repo-path",
            str(repo_path),
            "--db-path",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--confirm-approved-task",
            "--skip-preflight",
            "--json",
        ],
        cwd=repo_root,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {}
    return {
        "returncode": completed.returncode,
        "payload": payload,
        "stderr": completed.stderr,
    }


def _init_git_repo(repo_path: Path) -> None:
    commands = (
        ("git", "init", "-b", "main"),
        ("git", "config", "user.email", "m1c@example.invalid"),
        ("git", "config", "user.name", "M1-C Rehearsal"),
    )
    for command in commands:
        subprocess.run(
            command,
            cwd=repo_path,
            shell=False,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    (repo_path / "README.md").write_text("M1-C rehearsal\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=repo_path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


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
