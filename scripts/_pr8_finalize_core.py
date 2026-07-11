#!/usr/bin/env python3
"""One-shot PR-8 compatibility patch; removes itself after applying."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected pattern missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "agent_taskflow/reset_runtime_path.py",
    '''from agent_taskflow.runtime_admission import (\n    ActiveAttemptExistsError,\n    RuntimeClaim,\n)\n''',
    '''from agent_taskflow.runtime_admission import (\n    DEFAULT_LEASE_TTL_SECONDS,\n    ActiveAttemptExistsError,\n    RuntimeClaim,\n)\n''',
)
replace_once(
    "agent_taskflow/reset_runtime_path.py",
    '''        owner_id: str,\n        ttl_seconds: int,\n        executor: str | None,\n        model: str | None,\n        base_commit: str | None,\n        policy_version: str | None,\n        config_snapshot_hash: str | None,\n        prompt_template_version: str | None,\n        permission_profile: str | None,\n        worktree_path: str | Path | None,\n        artifact_root: str | Path | None,\n        reason_code: str,\n        metadata: dict[str, Any] | None,\n''',
    '''        owner_id: str,\n        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,\n        executor: str | None = None,\n        model: str | None = None,\n        base_commit: str | None = None,\n        policy_version: str | None = None,\n        config_snapshot_hash: str | None = None,\n        prompt_template_version: str | None = None,\n        permission_profile: str | None = None,\n        worktree_path: str | Path | None = None,\n        artifact_root: str | Path | None = None,\n        reason_code: str = "runtime_pickup_claimed",\n        metadata: dict[str, Any] | None = None,\n''',
)

idempotent_anchor = '''    current_store = store or TaskMirrorStore(request.db_path)\n    lineage_store = ResetLineageStore(current_store.db_path)\n    preview = lineage_store.preview(request.task_key)\n'''
idempotent_replacement = '''    current_store = store or TaskMirrorStore(request.db_path)\n    lineage_store = ResetLineageStore(current_store.db_path)\n\n    if request.request_id is not None:\n        existing = lineage_store.get_by_request_id(request.request_id)\n        if existing is not None:\n            if (\n                existing.task_key != request.task_key\n                or existing.reason != request.reason\n                or existing.actor != request.actor\n            ):\n                raise TaskStatusResetError(\n                    "request_id already belongs to a different reset request"\n                )\n            if (\n                request.expected_reset_generation is not None\n                and request.expected_reset_generation != existing.expected_generation\n            ):\n                raise TaskStatusResetError(\n                    "idempotent reset request generation does not match persisted lineage"\n                )\n            if (\n                request.expected_old_attempt_id is not None\n                and request.expected_old_attempt_id != existing.old_attempt_id\n            ):\n                raise TaskStatusResetError(\n                    "idempotent reset request old Attempt does not match persisted lineage"\n                )\n            payload = _audit_payload(request, existing)\n            artifact_path = lineage_store.audit_artifact_path(existing)\n            artifact_error: str | None = None\n            if artifact_path is not None and not artifact_path.exists():\n                try:\n                    atomic_write_json(artifact_path, payload, sort_keys=True)\n                except OSError as exc:\n                    artifact_error = f"{exc.__class__.__name__}: {exc}"\n                    lineage_store.append_artifact_failure(\n                        existing.reset_id,\n                        actor=request.actor,\n                        error=artifact_error,\n                    )\n                    artifact_path = None\n            return TaskStatusResetResult(\n                task_key=request.task_key,\n                from_status=request.from_status,\n                to_status=request.to_status,\n                reason=request.reason,\n                dry_run=False,\n                operator_confirmed=True,\n                mutated=False,\n                audit_artifact_path=artifact_path,\n                artifact_error=artifact_error,\n                reset_id=existing.reset_id,\n                request_id=existing.request_id,\n                old_attempt_id=existing.old_attempt_id,\n                new_attempt_id=existing.new_attempt_id,\n                expected_reset_generation=existing.expected_generation,\n                committed_reset_generation=existing.committed_generation,\n                next_attempt_number=int(\n                    existing.metadata.get("new_attempt_number", 1)\n                ),\n                idempotent_replay=True,\n            )\n\n    preview = lineage_store.preview(request.task_key)\n'''
replace_once(
    "agent_taskflow/task_status_reset.py",
    idempotent_anchor,
    idempotent_replacement,
)

replace_once(
    "tests/test_reset_lineage.py",
    '''        task = self.task_store.get_task(self.task_key)\n        assert task is not None\n        self.assertEqual(task.status, "queued")\n        self.assertEqual(task.active_attempt_id, lineage.new_attempt_id)\n        with closing(connect(self.db_path)) as conn:\n            attempt = conn.execute(\n''',
    '''        task = self.task_store.get_task(self.task_key)\n        assert task is not None\n        self.assertEqual(task.status, "queued")\n        with closing(connect(self.db_path)) as conn:\n            active_attempt_id = conn.execute(\n                "SELECT active_attempt_id FROM tasks WHERE task_key = ?",\n                (self.task_key,),\n            ).fetchone()[0]\n            attempt = conn.execute(\n''',
)
replace_once(
    "tests/test_reset_lineage.py",
    '''        self.assertEqual(attempt["status"], "created")\n''',
    '''        self.assertEqual(active_attempt_id, lineage.new_attempt_id)\n        self.assertEqual(attempt["status"], "created")\n''',
)

replace_once(
    "tests/test_task_status_reset.py",
    '''        artifact_path = self.artifact_dir / "task-status-reset.json"\n        self.assertEqual(json.loads(stdout)["audit_artifact_path"], str(artifact_path))\n        payload = json.loads(artifact_path.read_text(encoding="utf-8"))\n''',
    '''        result = json.loads(stdout)\n        artifact_path = Path(result["audit_artifact_path"])\n        self.assertEqual(artifact_path.parent.name, "reset-audit")\n        self.assertTrue(artifact_path.name.startswith("reset-"))\n        payload = json.loads(artifact_path.read_text(encoding="utf-8"))\n        self.assertEqual(payload["reset_id"], result["reset_id"])\n        self.assertEqual(payload["new_attempt_id"], result["new_attempt_id"])\n''',
)

(ROOT / ".github/workflows/ci.yml").write_text(
    '''name: CI\n\non:\n  pull_request:\n  push:\n    branches:\n      - main\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install package and dependencies\n        run: python -m pip install -e .\n\n      - name: Run unit tests\n        run: PYTHONPATH=. python -m unittest discover -s tests\n\n      - name: Compile sources\n        run: PYTHONPATH=. python -m compileall agent_taskflow scripts tests\n''',
    encoding="utf-8",
)
Path(__file__).unlink()
