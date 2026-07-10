"""Regression tests for PR-1 atomic permission and M0 status reconciliation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.atomic_write import atomic_write_bytes


REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_DOC = REPO_ROOT / "docs" / "m0-correctness-baseline-status.md"


class AtomicPermissionCloseoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def test_new_file_is_0644_under_standard_022_umask(self) -> None:
        target = self.tmp_path / "artifact.bin"
        previous_umask = os.umask(0o022)
        try:
            atomic_write_bytes(target, b"payload")
        finally:
            os.umask(previous_umask)

        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_overwrite_preserves_executable_mode(self) -> None:
        target = self.tmp_path / "artifact.bin"
        target.write_bytes(b"old")
        target.chmod(0o750)

        atomic_write_bytes(target, b"new")

        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)


class MilestoneZeroStatusDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = STATUS_DOC.read_text(encoding="utf-8")
        cls.normalized = " ".join(cls.text.split())
        cls.normalized_lower = cls.normalized.lower()

    def test_status_document_exists(self) -> None:
        self.assertTrue(STATUS_DOC.is_file())

    def test_closes_only_the_atomic_slice(self) -> None:
        for phrase in (
            "atomic_permission_slice = closed",
            "atomic_temp_policy = closed",
            "milestone_0 = open_blocked",
            "level_2_eligible = false",
            "A standard `0o022` umask therefore produces a `0o644` file.",
            "Executable permission bits on an existing regular file are preserved.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_records_all_remaining_m0_blockers(self) -> None:
        for phrase in (
            "one-active-attempt constraint",
            "Atomic attempt claim",
            "canonical runtime admission path",
            "Attempt-scoped branch, worktree, lock, PID, and artifact resources",
            "create a fresh worktree",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_preserves_no_exclusion_policy(self) -> None:
        for phrase in (
            "Never silently exclude atomic temp candidates",
            "must fail closed",
            "Cleanup is a separate, explicit, human-confirmed, auditable operation",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_does_not_claim_m0_or_level2_completion(self) -> None:
        for forbidden in (
            "milestone_0 = closed",
            "milestone_0 = complete",
            "level_2_eligible = true",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.normalized_lower)

        self.assertIn(
            "overall level 2 milestone 0 exit gate is **not complete**",
            self.normalized_lower,
        )


if __name__ == "__main__":
    unittest.main()
