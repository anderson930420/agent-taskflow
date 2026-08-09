from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.m1_exit_gate import audit_m1_exit_gate
from agent_taskflow.m1_project_class_control_rehearsal import (
    M1_PROJECT_CLASS_CONTROLS_SCHEMA_VERSION,
    run_m1_project_class_control_rehearsal,
)
from agent_taskflow.store import default_db_path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_m1_project_class_control_rehearsal.py"


class M1ProjectClassControlRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "disposable-state.db"
        self.evidence_dir = self.root / "evidence"
        self.output = self.evidence_dir / "project-class-control-rehearsal.json"

    def test_rehearsal_exercises_isolation_immediacy_and_append_only_controls(self) -> None:
        evidence = run_m1_project_class_control_rehearsal(
            repo_root=REPO_ROOT,
            db_path=self.db_path,
            output=self.output,
        )

        self.assertEqual(
            evidence["schema_version"], M1_PROJECT_CLASS_CONTROLS_SCHEMA_VERSION
        )
        for field in (
            "project_pause_denied_new_pickup",
            "project_pause_did_not_abort_existing_attempt",
            "project_pause_cleared",
            "task_class_initially_control_permitted",
            "task_class_disable_applied",
            "task_class_eligibility_denied_immediately",
            "task_class_disable_cleared",
            "task_class_disable_did_not_abort_existing_attempt",
            "unrelated_project_unaffected",
            "unrelated_task_class_unaffected",
            "alternate_level2_entrypoint_denied",
            "append_only_control_evidence_verified",
            "operator_attribution_verified",
        ):
            with self.subTest(field=field):
                self.assertIs(evidence[field], True)
        self.assertFalse(evidence["actual_auto_merge_enabled"])
        self.assertFalse(evidence["production_database_modified"])
        self.assertFalse(evidence["real_executor_invoked"])
        self.assertEqual(evidence["task_class_control_scope"], "class_global")
        fixture = evidence["fixture_identifiers"]
        self.assertEqual(len(fixture["projects"]), 2)
        self.assertEqual(len(fixture["task_classes"]), 2)
        self.assertEqual(json.loads(self.output.read_text()), evidence)

        audit = audit_m1_exit_gate(
            db_path=self.db_path,
            repo_root=REPO_ROOT,
            evidence_dir=self.evidence_dir,
        )
        gate = next(
            item
            for item in audit["gates"]
            if item["gate"] == "project_class_kill_switch"
        )
        self.assertEqual(gate["status"], "passed")
        self.assertFalse(audit["auto_merge_eligible"])

    def test_rehearsal_refuses_production_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "refuses the production database"):
            run_m1_project_class_control_rehearsal(
                repo_root=REPO_ROOT,
                db_path=default_db_path(),
                output=self.output,
            )

    def test_cli_runs_without_site_packages(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--repo-root",
                str(REPO_ROOT),
                "--db-path",
                str(self.db_path),
                "--output",
                str(self.output),
            ],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["evidence"]["project_pause_denied_new_pickup"])


if __name__ == "__main__":
    unittest.main()
