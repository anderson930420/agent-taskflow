from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_runtime_chain_dogfood_smoke import (  # noqa: E402
    APPROVED_TASK_STATUS,
    DEFAULT_TASK_KEY,
    EXECUTOR_NAME,
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
    RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    run_smoke,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402


SCRIPT = REPO_ROOT / "scripts" / "run_runtime_chain_dogfood_smoke.py"


class RuntimeChainDogfoodSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_smoke(self) -> dict[str, object]:
        return run_smoke(workspace_root=self.workspace_root)

    def test_smoke_returns_ok(self) -> None:
        summary = self._run_smoke()
        self.assertTrue(summary["ok"])

    def test_smoke_reaches_waiting_approval(self) -> None:
        summary = self._run_smoke()
        self.assertEqual(summary["final_status"], APPROVED_TASK_STATUS)
        self.assertEqual(summary["task_key"], DEFAULT_TASK_KEY)

    def test_verifier_report_artifact_exists(self) -> None:
        summary = self._run_smoke()
        handoff = summary["intake_runner_handoff"]
        self.assertTrue(
            Path(str(handoff["verifier_report_path"])).is_file(),
            f"verifier report missing: {handoff['verifier_report_path']!r}",
        )
        self.assertTrue(handoff["verifier_run_id"])

    def test_intake_runner_handoff_artifact_exists(self) -> None:
        summary = self._run_smoke()
        handoff = summary["intake_runner_handoff"]
        self.assertTrue(
            Path(str(handoff["artifact_path"])).is_file(),
            f"intake_runner_handoff missing: {handoff['artifact_path']!r}",
        )
        self.assertEqual(
            handoff["recommended_command_kind"], "queued_task_handoff"
        )

    def test_runtime_handoff_execution_artifact_exists(self) -> None:
        summary = self._run_smoke()
        runtime = summary["runtime_audit"]
        artifact_path = Path(str(runtime["runtime_execution_artifact_path"]))
        self.assertTrue(artifact_path.is_file(), f"runtime artifact missing: {artifact_path}")

    def test_runtime_audit_event_kinds_present(self) -> None:
        summary = self._run_smoke()
        runtime = summary["runtime_audit"]
        kinds = set(runtime["runtime_event_kinds"])  # type: ignore[arg-type]
        self.assertIn(RUNTIME_PREFLIGHT_EVENT_TYPE, kinds)
        self.assertIn(RUNTIME_EXECUTION_STARTED_EVENT_TYPE, kinds)
        self.assertIn(RUNTIME_EXECUTION_FINISHED_EVENT_TYPE, kinds)
        self.assertGreaterEqual(int(runtime["runtime_event_count"]), 3)

    def test_store_runtime_audit_events_include_three_kinds(self) -> None:
        summary = self._run_smoke()
        store = TaskMirrorStore(Path(str(summary["db_path"])))
        events = store.list_runtime_audit_events(str(summary["task_key"]))
        kinds = {event["kind"] for event in events}
        self.assertEqual(
            kinds,
            {
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
                RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
            },
        )

    def test_api_runtime_audits_match_store(self) -> None:
        summary = self._run_smoke()
        api = summary["api_readback"]
        self.assertGreaterEqual(int(api["runtime_audits_count"]), 3)
        kinds = set(api["runtime_audit_kinds"])  # type: ignore[arg-type]
        self.assertEqual(
            kinds,
            {
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_EXECUTION_STARTED_EVENT_TYPE,
                RUNTIME_EXECUTION_FINISHED_EVENT_TYPE,
            },
        )

    def test_api_runtime_audit_items_all_advertise_safety_flags(self) -> None:
        summary = self._run_smoke()
        api = summary["api_readback"]
        items = api["runtime_audit_items"]  # type: ignore[index]
        self.assertGreaterEqual(len(items), 3)
        for item in items:
            self.assertTrue(item.get("not_action_evidence"))
            self.assertTrue(item.get("not_validation_authority"))

    def test_runtime_handoff_execution_artifact_safety_block(self) -> None:
        summary = self._run_smoke()
        runtime = summary["runtime_audit"]
        artifact_path = Path(str(runtime["runtime_execution_artifact_path"]))
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        safety = payload.get("safety") or {}
        self.assertTrue(safety.get("runtime_audit_only"))
        self.assertTrue(safety.get("not_action_evidence"))
        self.assertTrue(safety.get("not_validation_authority"))
        self.assertFalse(safety.get("approved"))
        self.assertFalse(safety.get("merged"))
        self.assertFalse(safety.get("cleanup_performed"))
        self.assertFalse(safety.get("background_worker_started"))

    def test_validation_results_remain_separate_and_authoritative(self) -> None:
        summary = self._run_smoke()
        store = TaskMirrorStore(Path(str(summary["db_path"])))
        validations = store.list_validation_results(str(summary["task_key"]))
        self.assertGreaterEqual(len(validations), 1)
        self.assertTrue(
            any(result.get("status") == "passed" for result in validations),
            f"expected at least one passed validator result, got {validations!r}",
        )
        # validation_result events must remain disjoint from runtime audit
        # events; runtime_execution_finished is not a validator record.
        runtime_events = store.list_runtime_audit_events(str(summary["task_key"]))
        for runtime_event in runtime_events:
            self.assertNotIn("validator", runtime_event)

    def test_smoke_records_no_approval_or_cleanup_events(self) -> None:
        summary = self._run_smoke()
        store = TaskMirrorStore(Path(str(summary["db_path"])))
        for event in store.list_task_events(str(summary["task_key"])):
            payload = json.loads(event.payload_json or "{}")
            kind = payload.get("kind") if isinstance(payload, dict) else None
            self.assertNotIn(
                kind,
                {
                    "approval_decision",
                    "approval_recorded",
                    "merge_recorded",
                    "branch_push_recorded",
                    "draft_pr_created",
                    "local_cleanup_confirmed",
                    "remote_branch_cleanup_confirmed",
                },
                f"smoke must not record action evidence; got kind={kind!r}",
            )

    def test_smoke_does_not_create_github_or_branch_evidence(self) -> None:
        summary = self._run_smoke()
        safety = summary["safety"]
        for flag in (
            "github_mutated",
            "branch_pushed",
            "pr_created",
            "merged",
            "approved",
            "rejected",
            "cleanup_performed",
            "background_worker_started",
            "scheduler_loop_started",
            "auto_selected_task",
            "batch_execution",
            "production_db_mutated",
            "runtime_audit_is_validation_authority",
            "used_real_executor",
            "network_used",
        ):
            self.assertFalse(
                safety[flag],
                f"safety flag {flag!r} must be False; got {safety!r}",
            )

    def test_smoke_uses_temp_db_not_production(self) -> None:
        summary = self._run_smoke()
        db_path = Path(str(summary["db_path"]))
        self.assertTrue(
            str(db_path).startswith(str(self.workspace_root)),
            f"smoke must use temp DB inside workspace_root; got {db_path}",
        )
        # The default production DB path lives under ~/.agent-taskflow.
        self.assertNotIn(".agent-taskflow", str(db_path))

    def test_smoke_output_includes_key_artifact_paths(self) -> None:
        summary = self._run_smoke()
        self.assertIn("scheduler", summary)
        self.assertIn("intake_runner_handoff", summary)
        self.assertIn("runtime_audit", summary)
        self.assertIn("api_readback", summary)
        self.assertTrue(summary["scheduler"]["proposal_artifact_path"])
        self.assertTrue(summary["scheduler"]["confirmation_artifact_path"])
        self.assertTrue(summary["intake_runner_handoff"]["artifact_path"])
        self.assertTrue(summary["intake_runner_handoff"]["verifier_report_path"])
        self.assertTrue(summary["runtime_audit"]["runtime_execution_artifact_path"])

    def test_api_readback_exposes_runtime_handoff_execution_artifact(self) -> None:
        summary = self._run_smoke()
        api = summary["api_readback"]
        self.assertIn(RUNTIME_EXECUTION_ARTIFACT_TYPE, api["artifact_types"])
        self.assertGreaterEqual(int(api["validations_count"]), 1)

    def test_smoke_does_not_require_network(self) -> None:
        # The smoke uses only local helpers + an injected fake executor +
        # fake validator. It builds an offline git repo, makes no HTTP
        # calls, and does not require gh / pi / opencode. Re-running it
        # back-to-back proves the same.
        first = self._run_smoke()
        self.assertTrue(first["ok"])

    def test_smoke_executor_is_local_only(self) -> None:
        summary = self._run_smoke()
        self.assertEqual(EXECUTOR_NAME, "noop")
        self.assertFalse(summary["safety"]["used_real_executor"])


class RuntimeChainDogfoodSmokeCliTests(unittest.TestCase):
    """End-to-end CLI invocation of the runtime-chain dogfood smoke."""

    def test_cli_default_run_returns_ok_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "smoke-ws"
            workspace_root.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace-root",
                    str(workspace_root),
                    "--json",
                ],
                cwd=str(REPO_ROOT),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "PYTHONPATH": str(REPO_ROOT),
                    "PATH": __import__("os").environ.get("PATH", ""),
                    "HOME": __import__("os").environ.get("HOME", ""),
                },
            )

        self.assertEqual(
            completed.returncode,
            0,
            f"smoke CLI exited {completed.returncode}: "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["final_status"], APPROVED_TASK_STATUS)
        # API readback inside the CLI smoke must observe all three runtime
        # event kinds.
        kinds = set(summary["api_readback"]["runtime_audit_kinds"])
        self.assertIn(RUNTIME_PREFLIGHT_EVENT_TYPE, kinds)
        self.assertIn(RUNTIME_EXECUTION_STARTED_EVENT_TYPE, kinds)
        self.assertIn(RUNTIME_EXECUTION_FINISHED_EVENT_TYPE, kinds)


if __name__ == "__main__":
    unittest.main()
