"""The F9 CLI requires explicit binding, takes its validators from the policy,
and defaults to a preview (RULINGS 67 for the policy)."""

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
from execution_policy_support import policy_block, project_entry, release_tree, write_registry
from test_integration_tick import TickFixture, VALIDATORS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_integration_tick.py"
spec = importlib.util.spec_from_file_location("f9_tick_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class IntegrationTickScriptTests(TickFixture):
    def arguments(self):
        return ["--db-path", str(self.db_path), "--repo", "owner/repo",
                "--repo-path", str(self.fixture.repo)]

    def test_real_script_default_preview_reports_order_without_publishing(self):
        first, second = self.make_ticket(priority="low"), self.make_ticket(priority="critical")
        script = release_tree(self.root / "release", self.registry_path) / "scripts/run_integration_tick.py"
        completed = subprocess.run([sys.executable, str(script), *self.arguments()],
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["confirmation_required"])
        self.assertEqual([o["task_key"] for o in result["outcomes"]], [first.task_key, second.task_key])
        self.assertTrue(all(o["status"] == "dry_run" for o in result["outcomes"]))
        self.assertEqual(self.integration.list_pr_states(), [])
        self.assertEqual(result["execution_policy"]["project"], "fixture")
        self.assertEqual(result["validator_specs"][0]["name"], VALIDATORS[0].name)

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
                printed = json.loads(output.getvalue())
                # RULINGS 67: the output names the policy the validators came from.
                self.assertEqual(printed.pop("execution_policy")["project"], "fixture")
                self.assertEqual(printed, {"ok": ok})

    def run_refused(self, args):
        with patch.object(cli, "run_integration_tick") as tick, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(args), 2)
        tick.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        return payload

    def test_a_validator_config_file_is_refused_even_when_it_matches(self):
        # RULINGS 67: cron keeps no second validator list, matching or not.
        config = self.root / "validators.json"
        config.write_text(json.dumps([
            {"name": s.name, "command": list(s.command), "timeout_seconds": s.timeout_seconds}
            for s in VALIDATORS
        ]))
        payload = self.run_refused([*self.arguments(), "--validator-config", str(config)])
        self.assertEqual(payload["reason_code"], cli.VALIDATOR_CONFIG_REFUSED)

    def test_a_repository_without_a_valid_policy_fails_before_tick(self):
        cases = {
            "execution_policy_missing": project_entry(self.fixture.repo, github_repo="owner/repo"),
            "execution_policy_executor_not_allowed": project_entry(
                self.fixture.repo, github_repo="owner/repo",
                execution=policy_block(executor="manual"),
            ),
            "execution_policy_validators_empty": project_entry(
                self.fixture.repo, github_repo="owner/repo",
                execution=policy_block(integration_validators=[]),
            ),
            "execution_policy_project_not_registered": project_entry(
                self.fixture.repo, github_repo="owner/elsewhere", execution=policy_block(),
            ),
        }
        for code, entry in cases.items():
            with self.subTest(code=code):
                write_registry(self.registry_path, {"fixture": entry})
                self.assertEqual(self.run_refused(self.arguments())["reason_code"], code)

    def test_missing_explicit_database_argument_is_refused(self):
        completed = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--db-path", completed.stderr)
