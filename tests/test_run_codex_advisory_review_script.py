"""Tests for scripts/run_codex_advisory_review.py and its packaged CLI."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_codex_advisory_review.py"


def _load_cli_module():
    from agent_taskflow.cli import run_codex_advisory_review as module

    return module


class RunCodexAdvisoryReviewScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifact_dir = self.root / "artifacts" / "GH-9001"
        self.cli = _load_cli_module()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def base_args(self) -> list[str]:
        return [
            "--task-key",
            "GH-9001",
            "--artifact-dir",
            str(self.artifact_dir),
        ]

    def test_cli_generates_artifacts_in_temp_dir(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = self.cli.main(self.base_args())

        self.assertEqual(rc, 0)
        for name in (
            "codex-advisory-review-prompt.md",
            "codex-advisory-review.json",
            "codex-advisory-review.md",
        ):
            self.assertTrue((self.artifact_dir / name).is_file(), name)

        output = stdout.getvalue()
        self.assertIn(str(self.artifact_dir / "codex-advisory-review.json"), output)

        payload = json.loads(
            (self.artifact_dir / "codex-advisory-review.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["schema_version"], "codex_advisory_review.v1")
        self.assertEqual(payload["review_status"], "not_run")
        self.assertIs(payload["validation_authority"], False)
        self.assertIs(payload["human_review_required"], True)

    def test_no_mode_flag_defaults_to_dry_run(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.cli.main(self.base_args())

        self.assertEqual(rc, 0)
        payload = json.loads(
            (self.artifact_dir / "codex-advisory-review.json").read_text(encoding="utf-8")
        )
        self.assertIs(payload["dry_run"], True)
        self.assertIs(payload["confirm_run"], False)
        self.assertIs(payload["codex_cli_invoked"], False)
        self.assertFalse(
            (self.artifact_dir / "codex-advisory-review-stdout.txt").exists()
        )
        self.assertFalse(
            (self.artifact_dir / "codex-advisory-review-stderr.txt").exists()
        )

    def test_cli_accepts_repo_and_worktree_paths(self) -> None:
        repo = self.root / "repo"
        worktree = self.root / "worktree"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.cli.main(
                [
                    *self.base_args(),
                    "--repo-path",
                    str(repo),
                    "--worktree-path",
                    str(worktree),
                    "--dry-run",
                ]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(
            (self.artifact_dir / "codex-advisory-review.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["repo_path"], str(repo.resolve()))
        self.assertEqual(payload["worktree_path"], str(worktree.resolve()))

    def test_cli_rejects_unsafe_task_key(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self.cli.main(
                ["--task-key", "bad key!", "--artifact-dir", str(self.artifact_dir)]
            )
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", stderr.getvalue())

    def test_cli_does_not_invoke_subprocess(self) -> None:
        with mock.patch.object(subprocess, "run") as run_mock, mock.patch.object(
            subprocess, "Popen"
        ) as popen_mock:
            with contextlib.redirect_stdout(io.StringIO()):
                self.cli.main(self.base_args())
        run_mock.assert_not_called()
        popen_mock.assert_not_called()

    def test_help_flag_lists_expected_flags(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "--task-key",
            "--repo-path",
            "--worktree-path",
            "--artifact-dir",
            "--dry-run",
            "--confirm-run",
            "--codex-command",
            "--timeout-seconds",
        ):
            self.assertIn(flag, result.stdout, flag)
        lowered = result.stdout.lower()
        normalized_help = " ".join(lowered.split())
        self.assertIn("advisory only", lowered)
        self.assertIn("never deterministic validation authority", normalized_help)

    def test_codex_command_without_confirm_run_errors(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self.cli.main([*self.base_args(), "--codex-command", "codex"])
        self.assertEqual(rc, 1)
        self.assertIn("--confirm-run", stderr.getvalue())

    def test_confirm_run_invokes_subprocess_and_exits_zero(self) -> None:
        from agent_taskflow import codex_advisory_review as core

        fake = subprocess.CompletedProcess(
            args=["codex"], returncode=0, stdout='{"review_status": "looks_good"}', stderr=""
        )
        with mock.patch.object(core.subprocess, "run", return_value=fake) as run_mock:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self.cli.main([*self.base_args(), "--confirm-run"])
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        self.assertIs(run_mock.call_args.kwargs["shell"], False)
        payload = json.loads(
            (self.artifact_dir / "codex-advisory-review.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["review_status"], "looks_good")
        self.assertIs(payload["codex_cli_invoked"], True)
        for name in (
            "codex-advisory-review-stdout.txt",
            "codex-advisory-review-stderr.txt",
        ):
            self.assertTrue((self.artifact_dir / name).is_file(), name)

    def test_confirm_run_tool_error_still_exits_zero(self) -> None:
        from agent_taskflow import codex_advisory_review as core

        with mock.patch.object(
            core.subprocess, "run", side_effect=FileNotFoundError("codex")
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self.cli.main([*self.base_args(), "--confirm-run"])
        self.assertEqual(rc, 0)
        payload = json.loads(
            (self.artifact_dir / "codex-advisory-review.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["review_status"], "tool_error")

    def test_confirm_run_high_risk_exits_zero(self) -> None:
        from agent_taskflow import codex_advisory_review as core

        fake = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout='{"review_status": "high_risk", "risk_level": "high"}',
            stderr="",
        )
        with mock.patch.object(core.subprocess, "run", return_value=fake):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self.cli.main([*self.base_args(), "--confirm-run"])
        self.assertEqual(rc, 0)

    def test_invalid_timeout_exits_one(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self.cli.main(
                [*self.base_args(), "--confirm-run", "--timeout-seconds", "0"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", stderr.getvalue())

    def test_script_source_has_no_subprocess_or_mutation(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        cli_source = (
            REPO_ROOT / "agent_taskflow" / "cli" / "run_codex_advisory_review.py"
        ).read_text(encoding="utf-8")
        for needle in (
            "subprocess.run",
            "git push",
            "gh pr create",
            "--run-codex",
            "--approve",
            "--merge",
            "--cleanup",
        ):
            self.assertNotIn(needle, source, needle)
            self.assertNotIn(needle, cli_source, needle)


if __name__ == "__main__":
    unittest.main()
