#!/usr/bin/env python3
"""One-shot PR-8 canonical reset guard patch; removes itself after applying."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected pattern missing in {path}: {old[:140]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "agent_taskflow/reset_lineage.py",
    '''                cursor = conn.execute(\n                    """\n                    UPDATE tasks\n''',
    '''                conn.execute(\n                    """\n                    INSERT INTO reset_lineage_suppressions(\n                        task_id, reset_id, new_attempt_id, created_at\n                    ) VALUES (?, ?, ?, ?)\n                    """,\n                    (task["task_id"], reset_id, new_attempt_id, now),\n                )\n                cursor = conn.execute(\n                    """\n                    UPDATE tasks\n''',
)
replace_once(
    "agent_taskflow/reset_lineage.py",
    '''                conn.execute(\n                    """\n                    INSERT INTO task_events(\n                        task_key, event_type, source, message,\n                        payload_json, created_at\n                    ) VALUES (?, 'status_changed', ?, ?, ?, ?)\n                    """,\n                    (\n                        normalized,\n                        normalized_actor,\n                        "Operator reset reserved a new retry Attempt",\n                        json.dumps(\n                            {\n                                "status": "queued",\n                                "blocked_reason": None,\n                                "kind": "reset_lineage_reserved",\n                                "reset_id": reset_id,\n                                "old_attempt_id": old_attempt_id,\n                                "new_attempt_id": new_attempt_id,\n                                "reset_generation": committed_generation,\n                            },\n                            sort_keys=True,\n                        ),\n                        now,\n                    ),\n                )\n\n            record = self.get(reset_id)\n''',
    '''                conn.execute(\n                    """\n                    INSERT INTO task_events(\n                        task_key, event_type, source, message,\n                        payload_json, created_at\n                    ) VALUES (?, 'status_changed', ?, ?, ?, ?)\n                    """,\n                    (\n                        normalized,\n                        normalized_actor,\n                        "Operator reset reserved a new retry Attempt",\n                        json.dumps(\n                            {\n                                "status": "queued",\n                                "blocked_reason": None,\n                                "kind": "reset_lineage_reserved",\n                                "reset_id": reset_id,\n                                "old_attempt_id": old_attempt_id,\n                                "new_attempt_id": new_attempt_id,\n                                "reset_generation": committed_generation,\n                            },\n                            sort_keys=True,\n                        ),\n                        now,\n                    ),\n                )\n                conn.execute(\n                    "DELETE FROM reset_lineage_suppressions WHERE task_id = ?",\n                    (task["task_id"],),\n                )\n\n            record = self.get(reset_id)\n''',
)

replace_once(
    "tests/test_reset_lineage.py",
    '''from pathlib import Path\nimport sqlite3\nimport tempfile\nimport threading\nimport unittest\n''',
    '''from pathlib import Path\nimport sqlite3\nimport subprocess\nimport tempfile\nimport threading\nimport unittest\n''',
)
replace_once(
    "tests/test_reset_lineage.py",
    '''from agent_taskflow.reset_runtime_path import ResetAwareRuntimeAdmissionStore\n''',
    '''from agent_taskflow.reset_runtime_path import (\n    ResetAwareRuntimeAdmissionStore,\n    ResetLineageRuntimeTaskStore,\n)\n''',
)
replace_once(
    "tests/test_reset_lineage.py",
    '''        self.repo = self.root / "repo"\n        self.repo.mkdir()\n        self.artifact_base = self.root / "artifacts" / "AT-PR8-1"\n''',
    '''        self.repo = self.root / "repo"\n        self.repo.mkdir()\n        subprocess.run(\n            ["git", "init", "-b", "main"],\n            cwd=self.repo,\n            check=True,\n            stdout=subprocess.PIPE,\n            stderr=subprocess.PIPE,\n        )\n        subprocess.run(\n            ["git", "config", "user.email", "test@example.com"],\n            cwd=self.repo,\n            check=True,\n        )\n        subprocess.run(\n            ["git", "config", "user.name", "Test User"],\n            cwd=self.repo,\n            check=True,\n        )\n        (self.repo / "README.md").write_text("test\\n", encoding="utf-8")\n        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)\n        subprocess.run(\n            ["git", "commit", "-m", "initial"],\n            cwd=self.repo,\n            check=True,\n            stdout=subprocess.PIPE,\n            stderr=subprocess.PIPE,\n        )\n        self.artifact_base = self.root / "artifacts" / "AT-PR8-1"\n''',
)

insert_before = '''    def test_reset_lineage_events_are_append_only(self) -> None:\n'''
new_tests = '''    def test_raw_blocked_to_queued_update_requires_reset_lineage(self) -> None:\n        with self.assertRaisesRegex(\n            sqlite3.IntegrityError,\n            "reset lineage reservation required",\n        ):\n            self.task_store.update_task_status(\n                self.task_key,\n                "queued",\n                expected_current_status="blocked",\n            )\n        self.assertEqual(self.task_store.get_task(self.task_key).status, "blocked")\n\n    def test_reserved_attempt_receives_fresh_attempt_resources(self) -> None:\n        lineage, _ = ResetLineageStore(self.db_path).reserve_retry(\n            self.task_key,\n            reason="fresh resource retry",\n            actor="operator",\n            request_id="fresh-resource-request",\n            expected_generation=0,\n            expected_old_attempt_id=self.old_attempt_id,\n        )\n        runtime = ResetLineageRuntimeTaskStore(\n            self.db_path,\n            heartbeat_interval_seconds=60,\n        )\n        resource = runtime.preclaim_runtime(\n            self.task_key,\n            source="test-runtime",\n            base_branch="main",\n            worktree_root=self.repo / ".worktrees",\n            artifact_base_root=self.artifact_base,\n        )\n        workspace = runtime.prepare_attempt_workspace(self.task_key)\n        self.assertTrue(workspace.ok, workspace.summary)\n        self.assertEqual(resource.attempt_id, lineage.new_attempt_id)\n        self.assertIn(lineage.new_attempt_id, str(resource.worktree_path))\n        self.assertIn(lineage.new_attempt_id, str(resource.artifact_root))\n        self.assertEqual(len(AttemptStore(self.db_path).list_attempts(self.task_key)), 2)\n        runtime.update_task_status(\n            self.task_key,\n            "blocked",\n            source="test-runtime",\n            blocked_reason="test complete",\n        )\n\n'''
replace_once(
    "tests/test_reset_lineage.py",
    insert_before,
    new_tests + insert_before,
)

Path(__file__).unlink()
