"""Step 4 rehearsal and its evidence gate (V1 Step 4, SPEC §19.1-§19.4).

The rehearsal runs §19.1-§19.3 against a disposable database and writes JSON
evidence. Only that evidence, passing the gate, lets ``max_concurrent_tasks``
rise above 1.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.concurrency_gate import (
    CONCURRENCY_EVIDENCE_FILENAME,
    CONCURRENCY_REHEARSAL_SCHEMA_VERSION,
    REQUIRED_CONCURRENCY_CHECKS,
    evaluate_concurrency_evidence,
)
from agent_taskflow.concurrency_rehearsal import run_concurrency_rehearsal
from agent_taskflow.runtime_capacity import read_runtime_capacity, set_max_concurrent_tasks
from agent_taskflow.store import TaskMirrorStore, connect

REPO_ROOT = Path(__file__).resolve().parents[1]


class RehearsalEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.output = cls.root / "rehearsal"
        cls.evidence = run_concurrency_rehearsal(
            output_dir=cls.output,
            repo_root=REPO_ROOT,
            threads=6,
            processes=3,
        )
        cls.evidence_path = cls.output / CONCURRENCY_EVIDENCE_FILENAME

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_evidence_file_is_written_and_matches_the_return_value(self) -> None:
        on_disk = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, self.evidence)
        self.assertEqual(on_disk["schema_version"], CONCURRENCY_REHEARSAL_SCHEMA_VERSION)

    def test_every_required_check_passed(self) -> None:
        checks = self.evidence["checks"]
        for section, names in REQUIRED_CONCURRENCY_CHECKS.items():
            for name in names:
                with self.subTest(section=section, check=name):
                    self.assertIs(checks.get(name), True, self.evidence["details"])
        self.assertTrue(self.evidence["all_checks_passed"])

    def test_evidence_covers_19_1_19_2_and_19_3(self) -> None:
        self.assertEqual(
            sorted(REQUIRED_CONCURRENCY_CHECKS), ["19.1", "19.2", "19.3"]
        )
        for section in ("19.1", "19.2", "19.3"):
            self.assertIn(section, self.evidence["details"])

    def test_rehearsal_used_only_disposable_databases(self) -> None:
        self.assertTrue(self.evidence["disposable_database"])
        self.assertFalse(self.evidence["production_database_touched"])
        for path in self.evidence["databases"]:
            self.assertTrue(Path(path).is_relative_to(self.output), path)

    def test_evidence_binds_the_repository_sha(self) -> None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(self.evidence["repo_sha"], head)

    def test_contention_is_recorded_in_the_evidence(self) -> None:
        contention = self.evidence["details"]["19.2"]["contention"]
        self.assertGreaterEqual(contention["busy_waits"], 1)
        self.assertGreater(contention["lock_acquisitions"], 0)

    def test_gate_accepts_the_real_rehearsal_evidence(self) -> None:
        report = evaluate_concurrency_evidence(self.evidence_path, repo_root=REPO_ROOT)
        self.assertEqual(report["gate"], "passed", report)

    def test_real_evidence_unlocks_a_limit_above_one(self) -> None:
        db = self.root / "target.db"
        TaskMirrorStore(db).init_db()
        setting = set_max_concurrent_tasks(
            db, 2, actor="operator", evidence_path=self.evidence_path
        )
        self.assertEqual(setting.max_concurrent_tasks, 2)
        self.assertEqual(read_runtime_capacity(db).max_concurrent_tasks, 2)

    def test_tampered_evidence_is_blocked(self) -> None:
        tampered = self.root / "tampered.json"
        payload = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        payload["checks"]["atomic_claim_processes_explicit_single_winner"] = False
        tampered.write_text(json.dumps(payload), encoding="utf-8")
        report = evaluate_concurrency_evidence(tampered, repo_root=REPO_ROOT)
        self.assertEqual(report["gate"], "blocked")
        self.assertTrue(
            any("atomic_claim_processes_explicit_single_winner" in error for error in report["errors"])
        )


class RehearsalSafetyTests(unittest.TestCase):
    def test_output_directory_must_be_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            output.mkdir()
            (output / "leftover").write_text("x", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_concurrency_rehearsal(output_dir=output, repo_root=REPO_ROOT)

    def test_script_writes_evidence_and_never_opens_the_default_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            output = Path(tmp) / "out"
            env = {**os.environ, "HOME": str(home)}
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "run_concurrency_rehearsal.py"),
                    "--output-dir",
                    str(output),
                    "--threads",
                    "4",
                    "--processes",
                    "2",
                ],
                cwd=REPO_ROOT,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["gate"]["gate"], "passed")
            self.assertTrue((output / CONCURRENCY_EVIDENCE_FILENAME).is_file())
            self.assertFalse((home / ".agent-taskflow").exists())


if __name__ == "__main__":
    unittest.main()
