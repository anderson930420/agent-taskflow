"""Disposable M1-D project admission and class-governance rehearsal."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.lifecycle_control import RuntimeControlStore, RuntimePausedError
from agent_taskflow.lifecycle_runtime_path import LifecycleRuntimeAdmissionStore
from agent_taskflow.models import TaskRecord, utc_now_iso
from agent_taskflow.project_class_control_schema import (
    PROJECT_CLASS_CONTROLS_MIGRATION,
)
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.store import TaskMirrorStore, connect, default_db_path


M1_PROJECT_CLASS_CONTROLS_SCHEMA_VERSION = "m1_project_class_controls.v1"


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        shell=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _event_is_complete(event: Any) -> bool:
    return bool(
        event.scope_kind
        and event.scope_id
        and event.to_mode
        and event.reason_code
        and event.actor
        and event.generation >= 1
        and event.timestamp
        and isinstance(event.metadata, dict)
    )


def _append_only_guards_hold(db_path: Path, event_id: int) -> tuple[bool, bool]:
    update_rejected = False
    delete_rejected = False
    with closing(connect(db_path)) as conn:
        try:
            conn.execute(
                "UPDATE runtime_control_events SET actor = 'tampered' WHERE event_id = ?",
                (event_id,),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            update_rejected = True
        try:
            conn.execute(
                "DELETE FROM runtime_control_events WHERE event_id = ?",
                (event_id,),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            delete_rejected = True
    return update_rejected, delete_rejected


def run_m1_project_class_control_rehearsal(
    *,
    repo_root: str | Path,
    db_path: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve()
    db = Path(db_path).expanduser().resolve()
    destination = Path(output).expanduser().resolve()
    production = Path(default_db_path()).expanduser().resolve()
    if not repo.is_dir():
        raise NotADirectoryError(f"repository root does not exist: {repo}")
    if db == production:
        raise ValueError("M1-D implementation rehearsal refuses the production database")
    if db.exists():
        raise FileExistsError(f"disposable rehearsal database already exists: {db}")
    if destination.exists():
        raise FileExistsError(f"rehearsal evidence already exists: {destination}")
    db.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    fixture = {
        "projects": ["project-a", "project-b"],
        "task_classes": ["docs-only", "test-hardening"],
        "tasks": {
            "AT-M1D-A1": {"project": "project-a", "task_class": "docs-only"},
            "AT-M1D-A2": {
                "project": "project-a",
                "task_class": "test-hardening",
            },
            "AT-M1D-B1": {"project": "project-b", "task_class": "docs-only"},
        },
    }
    artifacts = db.parent / "fixture-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    mirror = TaskMirrorStore(db)
    mirror.init_db()
    for task_key, metadata in fixture["tasks"].items():
        mirror.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=metadata["project"],
                status="queued",
                repo_path=repo,
                artifact_dir=artifacts / task_key,
                executor="manual",
            )
        )
    attempts = AttemptStore(db)
    attempts.init_db()
    for task_key, metadata in fixture["tasks"].items():
        attempts.register_task_identity(
            task_key,
            task_class=metadata["task_class"],
            is_legacy=False,
        )

    controls = RuntimeControlStore(db)
    controls.init_db()
    admission = LifecycleRuntimeAdmissionStore(db)
    initial_a1 = controls.class_control_allows_auto_merge("AT-M1D-A1")
    initial_b1 = controls.class_control_allows_auto_merge("AT-M1D-B1")

    active_a2 = admission.claim("AT-M1D-A2", owner_id="m1d-existing-a2")
    project_pause = controls.pause(
        scope_kind="project",
        scope_id="project-a",
        actor="m1d-project-operator",
        metadata={"fixture": "project-pause-isolation"},
    )
    project_denied = False
    try:
        admission.claim("AT-M1D-A1", owner_id="m1d-denied-a1")
    except RuntimePausedError:
        project_denied = True
    alternate_denied = False
    try:
        RuntimeAdmissionStore(db).claim(
            "AT-M1D-A1", owner_id="m1d-alternate-entry"
        )
    except RuntimePausedError:
        alternate_denied = True
    unaffected_b1 = admission.claim("AT-M1D-B1", owner_id="m1d-unaffected-b1")
    a2_during_pause = attempts.get_attempt(active_a2.attempt_id)
    project_existing_unaffected = bool(
        a2_during_pause
        and a2_during_pause.is_active
        and a2_during_pause.status == "preparing"
    )
    project_clear = controls.clear(
        scope_kind="project",
        scope_id="project-a",
        actor="m1d-project-operator",
        metadata={"fixture": "project-pause-isolation"},
    )
    active_a1 = admission.claim("AT-M1D-A1", owner_id="m1d-resumed-a1")

    class_disable = controls.disable_task_class_governance(
        "docs-only",
        actor="m1d-governance-operator",
        metadata={"fixture": "class-global-disable"},
    )
    denied_a1 = controls.class_control_allows_auto_merge("AT-M1D-A1")
    denied_b1 = controls.class_control_allows_auto_merge("AT-M1D-B1")
    unaffected_a2 = controls.class_control_allows_auto_merge("AT-M1D-A2")
    controls.assert_not_killed("AT-M1D-A1", active_a1.attempt_id)
    a1_during_disable = attempts.get_attempt(active_a1.attempt_id)
    class_active_unaffected = bool(
        a1_during_disable
        and a1_during_disable.is_active
        and a1_during_disable.status == "preparing"
    )
    class_clear = controls.clear(
        scope_kind="task_class",
        scope_id="docs-only",
        actor="m1d-governance-operator",
        metadata={"fixture": "class-global-disable"},
    )
    restored_a1 = controls.class_control_allows_auto_merge("AT-M1D-A1")

    project_events = controls.list_control_events(
        scope_kind="project", scope_id="project-a"
    )
    class_events = controls.list_control_events(
        scope_kind="task_class", scope_id="docs-only"
    )
    immutable_update, immutable_delete = _append_only_guards_hold(
        db, project_events[0].event_id
    )
    events_complete = bool(project_events and class_events) and all(
        _event_is_complete(event) for event in (*project_events, *class_events)
    )
    operator_attribution = (
        [event.actor for event in project_events]
        == ["m1d-project-operator", "m1d-project-operator"]
        and [event.actor for event in class_events]
        == ["m1d-governance-operator", "m1d-governance-operator"]
    )

    evidence: dict[str, Any] = {
        "schema_version": M1_PROJECT_CLASS_CONTROLS_SCHEMA_VERSION,
        "migration": PROJECT_CLASS_CONTROLS_MIGRATION,
        "generated_at": utc_now_iso(),
        "repo_root": str(repo),
        "repo_sha": _git_head(repo),
        "database_path": str(db),
        "fixture_identifiers": fixture,
        "project_resolution_source": "tasks.project",
        "task_class_resolution_source": "tasks.task_class",
        "task_class_control_scope": "class_global",
        "project_pause_denied_new_pickup": project_denied,
        "project_pause_did_not_abort_existing_attempt": project_existing_unaffected,
        "project_pause_cleared": (
            project_clear.mode == "running" and active_a1 is not None
        ),
        "task_class_initially_control_permitted": (
            initial_a1.class_control_allows_auto_merge
            and initial_b1.class_control_allows_auto_merge
        ),
        "task_class_disable_applied": (
            class_disable.mode == "kill_requested"
            and class_disable.reason_code
            == "operator_task_class_governance_disabled"
        ),
        "task_class_eligibility_denied_immediately": (
            not denied_a1.class_control_allows_auto_merge
            and not denied_b1.class_control_allows_auto_merge
        ),
        "task_class_disable_cleared": (
            class_clear.mode == "running"
            and restored_a1.class_control_allows_auto_merge
            and not restored_a1.actual_auto_merge_enabled
        ),
        "task_class_disable_did_not_abort_existing_attempt": class_active_unaffected,
        "unrelated_project_unaffected": unaffected_b1 is not None,
        "unrelated_task_class_unaffected": (
            unaffected_a2.class_control_allows_auto_merge
            and not unaffected_a2.actual_auto_merge_enabled
        ),
        "alternate_level2_entrypoint_denied": alternate_denied,
        "append_only_control_evidence_verified": (
            events_complete and immutable_update and immutable_delete
        ),
        "operator_attribution_verified": operator_attribution,
        "project_control_generation": project_pause.generation,
        "task_class_control_generation": class_disable.generation,
        "active_attempt_ids": {
            "project_pause_existing": active_a2.attempt_id,
            "task_class_disable_existing": active_a1.attempt_id,
        },
        "actual_auto_merge_enabled": False,
        "production_database_modified": False,
        "real_executor_invoked": False,
        "scheduler_started": False,
        "os_signals_sent": False,
    }
    atomic_write_json(destination, evidence, indent=2, sort_keys=True)
    return evidence


__all__ = [
    "M1_PROJECT_CLASS_CONTROLS_SCHEMA_VERSION",
    "run_m1_project_class_control_rehearsal",
]
