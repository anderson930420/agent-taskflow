from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.atomic_temp_orphan_audit import (
    ATOMIC_TEMP_ORPHAN_AUDIT_SCHEMA_VERSION,
    ATOMIC_TEMP_ORPHAN_AUDIT_SOURCE,
    AtomicTempOrphanAuditRequest,
    render_atomic_temp_orphan_audit_summary,
    summarize_atomic_temp_orphans,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "summarize_atomic_temp_orphans.py"


class AtomicTempOrphanAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _audit(self, *, max_entries: int = 100) -> dict[str, object]:
        return summarize_atomic_temp_orphans(
            AtomicTempOrphanAuditRequest(
                roots=(self.root,),
                max_entries=max_entries,
            )
        )

    def test_detects_matching_orphan_and_computes_candidate_target(self) -> None:
        orphan = self.root / ".task-status-reset.json.0123456789abcdef.tmp"
        orphan.write_text("partial evidence", encoding="utf-8")

        audit = self._audit()

        self.assertTrue(audit["ok"])
        self.assertEqual(
            audit["schema_version"], ATOMIC_TEMP_ORPHAN_AUDIT_SCHEMA_VERSION
        )
        self.assertEqual(audit["source"], ATOMIC_TEMP_ORPHAN_AUDIT_SOURCE)
        self.assertEqual(audit["summary"]["orphan_temp_count"], 1)
        item = audit["orphan_temp_files"][0]
        self.assertEqual(item["path"], str(orphan))
        self.assertEqual(
            item["candidate_target_path"],
            str(self.root / "task-status-reset.json"),
        )
        self.assertEqual(item["candidate_target_name"], "task-status-reset.json")
        self.assertEqual(item["random_segment"], "0123456789abcdef")
        self.assertEqual(item["size_bytes"], len("partial evidence"))
        self.assertIsInstance(item["mtime_ns"], int)
        self.assertTrue(item["is_regular_file"])

    def test_ignores_nonmatching_files(self) -> None:
        names = (
            "task-status-reset.json.tmp",
            ".task-status-reset.json.123.tmp",
            ".task-status-reset.json.nothexvalue.tmp",
            ".task-status-reset.json.0123456789abcdeg.tmp",
        )
        for name in names:
            (self.root / name).write_text(name, encoding="utf-8")

        audit = self._audit()

        self.assertEqual(audit["summary"]["orphan_temp_count"], 0)
        self.assertEqual(audit["orphan_temp_files"], [])

    def test_audit_is_read_only_and_reports_safety_flags(self) -> None:
        orphan = self.root / ".evidence.json.0123456789abcdef.tmp"
        unrelated = self.root / "keep-me.txt"
        orphan.write_text("orphan", encoding="utf-8")
        unrelated.write_text("unrelated", encoding="utf-8")

        audit = self._audit()

        self.assertTrue(orphan.exists())
        self.assertEqual(orphan.read_text(encoding="utf-8"), "orphan")
        self.assertTrue(unrelated.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "unrelated")
        safety = audit["safety"]
        self.assertTrue(safety["read_only"])
        for key in (
            "files_deleted",
            "files_modified",
            "db_written",
            "gitignore_modified",
            "changed_files_validator_modified",
            "changed_files_exclusion_added",
            "cleanup_performed",
            "executor_started",
            "validator_started",
            "approved",
            "merged",
        ):
            self.assertFalse(safety[key], key)

    def test_max_entries_truncates_results_but_preserves_total_count(self) -> None:
        for index in range(3):
            name = f".evidence-{index}.json.{index:016x}.tmp"
            (self.root / name).write_text(str(index), encoding="utf-8")

        audit = self._audit(max_entries=2)

        self.assertEqual(audit["summary"]["orphan_temp_count"], 3)
        self.assertEqual(len(audit["orphan_temp_files"]), 2)
        self.assertTrue(audit["summary"]["truncated"])

    def test_human_renderer_includes_orphan_count_and_candidate_path(self) -> None:
        (self.root / ".evidence.json.0123456789abcdef.tmp").write_text(
            "orphan", encoding="utf-8"
        )

        rendered = render_atomic_temp_orphan_audit_summary(self._audit())

        self.assertIn("Orphan temp files (1):", rendered)
        self.assertIn(str(self.root / "evidence.json"), rendered)
        self.assertIn("read-only", rendered)

    def test_missing_root_is_warning_and_does_not_fail_audit(self) -> None:
        missing = self.root / "missing"

        audit = summarize_atomic_temp_orphans(
            AtomicTempOrphanAuditRequest(roots=(missing,))
        )

        self.assertTrue(audit["ok"])
        self.assertEqual(audit["summary"]["warning_count"], 1)
        self.assertIn(str(missing), audit["warnings"][0])


class AtomicTempOrphanAuditScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.orphan = self.root / ".task-status-reset.json.0123456789abcdef.tmp"
        self.orphan.write_text("orphan", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_json_mode(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(self.root),
                "--json",
            ],
            cwd=str(REPO_ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["orphan_temp_count"], 1)
        self.assertEqual(payload["orphan_temp_files"][0]["path"], str(self.orphan))
        self.assertTrue(self.orphan.exists())

    def test_direct_path_help_without_pythonpath(self) -> None:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=str(self.root),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--root", result.stdout)
        self.assertIn("--max-entries", result.stdout)


if __name__ == "__main__":
    unittest.main()
