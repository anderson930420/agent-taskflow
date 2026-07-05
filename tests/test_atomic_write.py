"""Tests for the atomic artifact write helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.atomic_write import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)


class AtomicWriteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def temp_leftovers(self, directory: Path) -> list[Path]:
        return [path for path in directory.iterdir() if path.name.endswith(".tmp")]


class AtomicWriteBytesTests(AtomicWriteTestCase):
    def test_writes_content_and_returns_target(self) -> None:
        target = self.tmp_path / "artifact.bin"
        returned = atomic_write_bytes(target, b"payload-bytes")
        self.assertEqual(returned, target)
        self.assertEqual(target.read_bytes(), b"payload-bytes")

    def test_creates_missing_parent_directories(self) -> None:
        target = self.tmp_path / "nested" / "deeper" / "artifact.bin"
        atomic_write_bytes(target, b"data")
        self.assertEqual(target.read_bytes(), b"data")

    def test_overwrites_existing_file(self) -> None:
        target = self.tmp_path / "artifact.bin"
        target.write_bytes(b"old")
        atomic_write_bytes(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")

    def test_leaves_no_temp_files_on_success(self) -> None:
        target = self.tmp_path / "artifact.bin"
        atomic_write_bytes(target, b"data")
        self.assertEqual(self.temp_leftovers(self.tmp_path), [])

    def test_failed_replace_keeps_previous_target_intact(self) -> None:
        target = self.tmp_path / "artifact.bin"
        target.write_bytes(b"previous-complete-content")
        with patch(
            "agent_taskflow.atomic_write.os.replace",
            side_effect=OSError("simulated crash before replace"),
        ):
            with self.assertRaises(OSError):
                atomic_write_bytes(target, b"partial-new-content")
        self.assertEqual(target.read_bytes(), b"previous-complete-content")

    def test_failed_replace_cleans_up_temp_file(self) -> None:
        target = self.tmp_path / "artifact.bin"
        with patch(
            "agent_taskflow.atomic_write.os.replace",
            side_effect=OSError("simulated crash before replace"),
        ):
            with self.assertRaises(OSError):
                atomic_write_bytes(target, b"data")
        self.assertFalse(target.exists())
        self.assertEqual(self.temp_leftovers(self.tmp_path), [])


class AtomicWriteTextTests(AtomicWriteTestCase):
    def test_writes_text_utf8_by_default(self) -> None:
        target = self.tmp_path / "artifact.txt"
        atomic_write_text(target, "hello — unicode\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello — unicode\n")

    def test_honors_explicit_encoding(self) -> None:
        target = self.tmp_path / "artifact.txt"
        atomic_write_text(target, "héllo", encoding="latin-1")
        self.assertEqual(target.read_bytes(), "héllo".encode("latin-1"))

    def test_failed_encode_keeps_previous_target_and_no_temp(self) -> None:
        target = self.tmp_path / "artifact.txt"
        target.write_text("previous", encoding="utf-8")
        with self.assertRaises(UnicodeEncodeError):
            atomic_write_text(target, "smiley \N{GRINNING FACE}", encoding="ascii")
        self.assertEqual(target.read_text(encoding="utf-8"), "previous")
        self.assertEqual(self.temp_leftovers(self.tmp_path), [])


class AtomicWriteJsonTests(AtomicWriteTestCase):
    def test_default_formatting_matches_legacy_artifact_writes(self) -> None:
        # Most artifact writes used: json.dumps(payload, indent=2, ...) + "\n".
        target = self.tmp_path / "artifact.json"
        payload = {"b": 2, "a": 1}
        atomic_write_json(target, payload, sort_keys=True)
        expected = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.assertEqual(target.read_text(encoding="utf-8"), expected)

    def test_no_trailing_newline_matches_legacy_audit_writes(self) -> None:
        # The changed-files audit wrote json.dumps(...) without a newline.
        target = self.tmp_path / "audit.json"
        payload = {"violations": []}
        atomic_write_json(target, payload, sort_keys=True, trailing_newline=False)
        expected = json.dumps(payload, indent=2, sort_keys=True)
        self.assertEqual(target.read_text(encoding="utf-8"), expected)

    def test_written_json_round_trips(self) -> None:
        target = self.tmp_path / "artifact.json"
        payload = {"task_key": "AT-GH-128", "changed_files": ["a.py"], "ok": True}
        atomic_write_json(target, payload)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)

    def test_non_serializable_payload_keeps_previous_target_and_no_temp(self) -> None:
        target = self.tmp_path / "artifact.json"
        target.write_text('{"previous": true}\n', encoding="utf-8")
        with self.assertRaises(TypeError):
            atomic_write_json(target, {"bad": object()})
        self.assertEqual(target.read_text(encoding="utf-8"), '{"previous": true}\n')
        self.assertEqual(self.temp_leftovers(self.tmp_path), [])


if __name__ == "__main__":
    unittest.main()
