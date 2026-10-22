"""Tests for scripts/create_ticket.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_ticket.py"

RESPONSE = {"task_key": "AT-0002", "display_status": "ready", "repository": "agent-taskflow"}


def _load_script_module():
    spec = importlib.util.spec_from_file_location("create_ticket", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_response(payload: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    return response


class CreateTicketScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script_module()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_TASKFLOW_API_BASE_URL", None)

    def run_main(self, argv: list[str], urlopen: mock.MagicMock) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("urllib.request.urlopen", urlopen), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr):
            code = self.module.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def sent_request(self, urlopen: mock.MagicMock):
        urlopen.assert_called_once()
        return urlopen.call_args.args[0]

    def test_success_prints_task_key_and_status(self) -> None:
        urlopen = mock.MagicMock(return_value=_fake_response(RESPONSE))
        code, out, _ = self.run_main(
            ["--repository", "agent-taskflow", "--priority", "high", "Fix the docs"], urlopen
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "AT-0002 ready\n")
        request = self.sent_request(urlopen)
        self.assertEqual(request.full_url, "http://127.0.0.1:8100/api/tickets")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"repository": "agent-taskflow", "prompt": "Fix the docs", "priority": "high"},
        )

    def test_priority_defaults_to_normal(self) -> None:
        urlopen = mock.MagicMock(return_value=_fake_response(RESPONSE))
        self.run_main(["--repository", "agent-taskflow", "Fix the docs"], urlopen)
        body = json.loads(self.sent_request(urlopen).data.decode("utf-8"))
        self.assertEqual(body["priority"], "normal")

    def test_json_prints_full_response(self) -> None:
        urlopen = mock.MagicMock(return_value=_fake_response(RESPONSE))
        code, out, _ = self.run_main(
            ["--repository", "agent-taskflow", "--json", "Fix the docs"], urlopen
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), RESPONSE)

    def test_prompt_file_is_read(self) -> None:
        urlopen = mock.MagicMock(return_value=_fake_response(RESPONSE))
        with TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text("Prompt from a file\n", encoding="utf-8")
            code, _, _ = self.run_main(
                ["--repository", "agent-taskflow", "--prompt-file", str(prompt_file)], urlopen
            )
        self.assertEqual(code, 0)
        body = json.loads(self.sent_request(urlopen).data.decode("utf-8"))
        self.assertEqual(body["prompt"], "Prompt from a file\n")

    def test_env_base_url_and_trailing_slash(self) -> None:
        urlopen = mock.MagicMock(return_value=_fake_response(RESPONSE))
        with mock.patch.dict("os.environ", {"AGENT_TASKFLOW_API_BASE_URL": "http://api.test:9000//"}):
            self.run_main(["--repository", "agent-taskflow", "Fix the docs"], urlopen)
        self.assertEqual(self.sent_request(urlopen).full_url, "http://api.test:9000/api/tickets")

    def test_explicit_base_url_trailing_slash_is_stripped(self) -> None:
        urlopen = mock.MagicMock(return_value=_fake_response(RESPONSE))
        with mock.patch.dict("os.environ", {"AGENT_TASKFLOW_API_BASE_URL": "http://ignored.test"}):
            self.run_main(
                ["--repository", "agent-taskflow", "--api-base-url", "http://explicit.test/", "x"],
                urlopen,
            )
        self.assertEqual(self.sent_request(urlopen).full_url, "http://explicit.test/api/tickets")

    def test_http_error_with_json_detail_returns_1(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8100/api/tickets",
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(json.dumps({"detail": "unknown repository: nope"}).encode("utf-8")),
        )
        urlopen = mock.MagicMock(side_effect=error)
        code, out, err = self.run_main(["--repository", "nope", "Fix the docs"], urlopen)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("422", err)
        self.assertIn("unknown repository: nope", err)

    def test_http_error_with_raw_body_returns_1(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8100/api/tickets", 500, "Server Error", {}, io.BytesIO(b"boom")
        )
        urlopen = mock.MagicMock(side_effect=error)
        code, _, err = self.run_main(["--repository", "agent-taskflow", "x"], urlopen)
        self.assertEqual(code, 1)
        self.assertIn("500", err)
        self.assertIn("boom", err)

    def test_url_error_returns_2_and_names_base_url(self) -> None:
        urlopen = mock.MagicMock(side_effect=urllib.error.URLError("connection refused"))
        code, out, err = self.run_main(["--repository", "agent-taskflow", "x"], urlopen)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("http://127.0.0.1:8100", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_both_prompt_sources_is_an_argparse_error(self) -> None:
        urlopen = mock.MagicMock()
        with TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text("x", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(
                    ["--repository", "agent-taskflow", "--prompt-file", str(prompt_file), "x"],
                    urlopen,
                )
        self.assertEqual(ctx.exception.code, 2)
        urlopen.assert_not_called()

    def test_no_prompt_source_is_an_argparse_error(self) -> None:
        urlopen = mock.MagicMock()
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(["--repository", "agent-taskflow"], urlopen)
        self.assertEqual(ctx.exception.code, 2)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
