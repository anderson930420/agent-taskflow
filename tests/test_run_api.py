"""Tests for scripts/run_api.py."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Ensure the repo root is in sys.path before importing the script modules
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestRunApiScript(unittest.TestCase):
    """Test the run_api.py script argument parsing and invocation."""

    def test_argument_parser_accepts_db_path(self) -> None:
        """Verify --db-path is a required argument."""
        from scripts.run_api import build_parser

        parser = build_parser()
        # Should not raise when --db-path is provided
        args = parser.parse_args(["--db-path", "/tmp/test.db"])
        self.assertEqual(args.db_path, "/tmp/test.db")

    def test_default_host_is_127_0_0_1(self) -> None:
        """Verify default host is 127.0.0.1."""
        from scripts.run_api import build_parser

        parser = build_parser()
        args = parser.parse_args(["--db-path", "/tmp/test.db"])
        self.assertEqual(args.host, "127.0.0.1")

    def test_default_port_is_8100(self) -> None:
        """Verify default port is 8100."""
        from scripts.run_api import build_parser

        parser = build_parser()
        args = parser.parse_args(["--db-path", "/tmp/test.db"])
        self.assertEqual(args.port, 8100)

    def test_default_log_level_is_warning(self) -> None:
        """Verify default log-level is warning."""
        from scripts.run_api import build_parser

        parser = build_parser()
        args = parser.parse_args(["--db-path", "/tmp/test.db"])
        self.assertEqual(args.log_level, "warning")

    def test_custom_host_and_port(self) -> None:
        """Verify custom host and port are accepted."""
        from scripts.run_api import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "--db-path", "/tmp/test.db",
            "--host", "0.0.0.0",
            "--port", "9000",
        ])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9000)

    def test_custom_log_level(self) -> None:
        """Verify custom log-level is accepted."""
        from scripts.run_api import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "--db-path", "/tmp/test.db",
            "--log-level", "info",
        ])
        self.assertEqual(args.log_level, "info")

    @patch("scripts.run_api.uvicorn")
    @patch("scripts.run_api.create_app")
    def test_create_app_called_with_db_path(
        self,
        mock_create_app: MagicMock,
        mock_uvicorn: MagicMock,
    ) -> None:
        """Verify create_app is called with the db_path argument."""
        from scripts.run_api import main

        mock_app = MagicMock()
        mock_create_app.return_value = mock_app

        main(["--db-path", "/tmp/test.db"])

        mock_create_app.assert_called_once_with(db_path="/tmp/test.db")

    @patch("scripts.run_api.uvicorn")
    @patch("scripts.run_api.create_app")
    def test_uvicorn_run_called_with_expected_parameters(
        self,
        mock_create_app: MagicMock,
        mock_uvicorn: MagicMock,
    ) -> None:
        """Verify uvicorn.run is called with expected parameters."""
        from scripts.run_api import main

        mock_app = MagicMock()
        mock_create_app.return_value = mock_app

        main([
            "--db-path", "/tmp/test.db",
            "--host", "0.0.0.0",
            "--port", "9000",
            "--log-level", "info",
        ])

        mock_uvicorn.run.assert_called_once_with(
            mock_app,
            host="0.0.0.0",
            port=9000,
            log_level="info",
        )

    @patch("scripts.run_api.uvicorn")
    @patch("scripts.run_api.create_app")
    def test_uvicorn_run_called_with_defaults(
        self,
        mock_create_app: MagicMock,
        mock_uvicorn: MagicMock,
    ) -> None:
        """Verify uvicorn.run uses default host/port/log-level."""
        from scripts.run_api import main

        mock_app = MagicMock()
        mock_create_app.return_value = mock_app

        main(["--db-path", "/tmp/test.db"])

        mock_uvicorn.run.assert_called_once_with(
            mock_app,
            host="127.0.0.1",
            port=8100,
            log_level="warning",
        )

    def test_importing_script_does_not_auto_start_server(self) -> None:
        """Verify that importing the script does not start the server.

        This is important because uvicorn.run is blocking and would hang
        the tests if called at import time.
        """
        # Re-importing should not raise or start any server
        import importlib
        import scripts.run_api
        importlib.reload(scripts.run_api)
        # If we get here without the server starting, the test passes


class TestArgumentParserValidation(unittest.TestCase):
    """Test argument parser validation."""

    def test_db_path_is_required(self) -> None:
        """Verify --db-path is required and parser fails without it."""
        from scripts.run_api import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_invalid_log_level_rejected(self) -> None:
        """Verify invalid log-level is rejected."""
        from scripts.run_api import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "--db-path", "/tmp/test.db",
                "--log-level", "invalid",
            ])

    def test_port_must_be_integer(self) -> None:
        """Verify port must be an integer."""
        from scripts.run_api import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "--db-path", "/tmp/test.db",
                "--port", "not-a-number",
            ])


if __name__ == "__main__":
    unittest.main()