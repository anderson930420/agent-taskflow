"""The F9 CLI requires explicit binding/config and defaults to a preview."""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_integration_tick import TickFixture, VALIDATORS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_integration_tick.py"
spec = importlib.util.spec_from_file_location("f9_tick_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class IntegrationTickScriptTests(TickFixture):
    def arguments(self):
        config = self.root / "validators.json"
        config.write_text(json.dumps([
            {"name": s.name, "command": list(s.command), "timeout_seconds": s.timeout_seconds}
            for s in VALIDATORS
        ]))
        return ["--db-path", str(self.db_path), "--repo", "owner/repo",
                "--repo-path", str(self.fixture.repo), "--validator-config", str(config)]

    def test_real_script_default_preview_reports_order_without_publishing(self):
        first, second = self.make_ticket(priority="low"), self.make_ticket(priority="critical")
        completed = subprocess.run([sys.executable, str(SCRIPT), *self.arguments()],
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["confirmation_required"])
        self.assertEqual([o["task_key"] for o in result["outcomes"]], [first.task_key, second.task_key])
        self.assertTrue(all(o["status"] == "dry_run" for o in result["outcomes"]))
        self.assertEqual(self.integration.list_pr_states(), [])

    def test_explicit_confirm_maps_to_controller_confirmation_and_exit_status(self):
        for ok, exit_code in ((True, 0), (False, 1)):
            with self.subTest(ok=ok), patch.object(cli, "run_integration_tick", return_value={"ok": ok}) as tick:
                with redirect_stdout(io.StringIO()) as output:
                    actual = cli.main([*self.arguments(), "--confirm-integration"])
                self.assertEqual(actual, exit_code)
                request = tick.call_args.args[0]
                self.assertFalse(request.dry_run)
                self.assertTrue(request.confirm_integration)
                self.assertEqual(request.validator_specs, VALIDATORS)
                self.assertEqual(json.loads(output.getvalue()), {"ok": ok})

    def test_invalid_config_fails_before_tick_with_json_error(self):
        for config in ([], {}, [{"name": "x", "command": "true"}],
                       [{"name": "x", "command": []}],
                       [{"name": "x", "command": ["true"], "timeout_seconds": False}],
                       [{"name": "x", "command": ["true"], "shell": True}],
                       [{"name": "x", "command": ["true"]}] * 2):
            with self.subTest(config=config):
                args = self.arguments()
                Path(args[-1]).write_text(json.dumps(config))
                with patch.object(cli, "run_integration_tick") as tick, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(args), 2)
                tick.assert_not_called()
                self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_missing_explicit_database_argument_is_refused(self):
        completed = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--db-path", completed.stderr)
