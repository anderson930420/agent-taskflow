"""Regression tests for atomic-write file permissions."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.atomic_write import atomic_write_bytes


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


if __name__ == "__main__":
    unittest.main()
