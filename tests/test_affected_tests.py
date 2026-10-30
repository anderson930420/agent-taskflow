from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.affected_tests import changed_paths, select_tests
from agent_taskflow.validators.affected_tests import AffectedTestsValidator
from agent_taskflow.validators.base import ValidatorContext


class AffectedTestsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        self.write('README.md', 'base')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, text=True)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_selection_rules(self):
        self.write('tests/test_self.py', '')
        self.write('tests/test_named_extra.py', '')
        self.write('tests/test_import.py', 'import agent_taskflow.x.mod\nfrom agent_taskflow.x import other\n')
        self.write('tests/test_helper_user.py', 'from tests import helper\n')
        self.write('tests/test_script_user.py', 'path = "scripts/foo.py"\n')
        self.write('tests/test_config.py', 'path = "config/projects.yaml"\n')
        cases = {
            'tests/test_self.py': 'tests/test_self.py',
            'tests/helper.py': 'tests/test_helper_user.py',
            'agent_taskflow/named.py': 'tests/test_named_extra.py',
            'agent_taskflow/x/mod.py': 'tests/test_import.py',
            'agent_taskflow/x/other.py': 'tests/test_import.py',
            'scripts/foo.py': 'tests/test_script_user.py',
            'config/projects.yaml': 'tests/test_config.py',
        }
        for change, test in cases.items():
            with self.subTest(change=change):
                self.assertIn(test, select_tests(self.root, {change}))
        self.assertEqual(select_tests(self.root, {'docs/readme.md'}), {})
        self.assertEqual(select_tests(self.root, set()), {})
        self.write('tests/test_v0_supported_surface.py', '')
        self.assertIn('tests/test_v0_supported_surface.py', select_tests(self.root, {'constraints.txt'}))

    def test_api_change_selects_http_route_tests(self):
        self.write('tests/test_http_only.py', 'response = client.post("/api/tickets", json={})\n')
        self.write('tests/test_app_import.py', 'from agent_taskflow.api.main import create_app\n')
        self.write('tests/test_unrelated.py', 'import agent_taskflow.store\n')
        selection = select_tests(self.root, {'agent_taskflow/api/tickets.py'})
        self.assertEqual(selection['tests/test_http_only.py'], ['api route changed: agent_taskflow/api/tickets.py'])
        self.assertIn('api route changed: agent_taskflow/api/tickets.py', selection['tests/test_app_import.py'])
        self.assertNotIn('tests/test_unrelated.py', selection)
        selection = select_tests(self.root, {'agent_taskflow/store.py'})
        self.assertNotIn('tests/test_http_only.py', selection)
        self.assertNotIn('tests/test_app_import.py', selection)
        self.assertTrue(all(not reason.startswith('api route changed')
                            for reasons in selection.values() for reason in reasons))

    def test_git_changes_both_phases(self):
        self.write('committed.py', '')
        self.git('add', 'committed.py')
        self.git('commit', '-qm', 'change')
        self.write('staged.py', '')
        self.git('add', 'staged.py')
        self.write('README.md', 'modified')
        self.write('untracked.py', '')
        expected = {'committed.py', 'staged.py', 'README.md', 'untracked.py'}
        self.assertEqual(changed_paths(self.root, base_commit=self.base), expected)
        self.assertEqual(changed_paths(self.root, target_ref=self.base), expected)
        self.git('rm', '-q', 'committed.py')
        self.assertIn('committed.py', changed_paths(self.root, target_ref=self.base))

    def test_validator_result_and_log(self):
        self.write('tests/test_a.py', 'def test_a(): pass\n')
        context = ValidatorContext(task_key='TEST-1', project='test', worktree_path=self.root,
                                   artifact_dir=self.root / 'artifacts', base_commit=self.base)
        for code, status in [(0, 'passed'), (1, 'failed'), (5, 'passed')]:
            def run(**kwargs):
                log = kwargs['artifact_dir'] / 'affected-tests.log'
                log.write_text('pytest output')
                return subprocess.CompletedProcess([], code), log, '', '', {}
            with self.subTest(code=code), patch('agent_taskflow.validators.affected_tests.run_command', side_effect=run):
                result = AffectedTestsValidator().run(context)
                self.assertEqual(result.status, status)
                self.assertIn('changed test: tests/test_a.py', result.log_path.read_text())
        (self.root / 'tests/test_a.py').unlink()
        result = AffectedTestsValidator().run(context)
        self.assertEqual(result.summary, 'no affected tests')

    def test_script(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/run_affected_tests.py'
        result = subprocess.run([sys.executable, str(script), '--base', self.base, '--repo-root', str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no affected tests', result.stdout)
        self.write('tests/test_small.py', 'def test_small(): assert True\n')
        result = subprocess.run([sys.executable, str(script), '--base', self.base, '--repo-root', str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tests/test_small.py', result.stdout)


if __name__ == '__main__':
    unittest.main()
