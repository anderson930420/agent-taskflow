import json
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.executors import ClaudeCodeExecutor, ExecutorContext
from agent_taskflow.ticket_usage import summarize_ticket_usage

SAMPLE = Path(__file__).parent / 'fixtures' / 'claude-json-sample.json'


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = ExecutorContext(task_key='AT-1', project='test', worktree_path=self.root,
                                       repo_root=self.root, artifact_dir=self.root / 'artifacts',
                                       attempt_id='attempt-1')
        self.executor = ClaudeCodeExecutor(command=['claude'], enable_invocation=True)

    def run_process(self, outcome):
        with patch('agent_taskflow.executors.claude_code.subprocess.run', side_effect=[outcome, subprocess.CompletedProcess(['git'], 0, b'')]):
            return self.executor.run(self.context)

    def test_real_sample_and_non_json(self):
        raw = SAMPLE.read_text()
        result = self.run_process(subprocess.CompletedProcess(['claude'], 0, raw, ''))
        data = json.loads(result.artifacts['claude_code_usage'].read_text())
        self.assertEqual((data['input_tokens'], data['output_tokens'], data['total_cost_usd']), (2, 4, 0.0461036))
        self.assertEqual(data['model_usage']['claude-opus-5-5']['cacheReadInputTokens'], 10238)
        self.assertEqual(result.log_path.read_text(), raw)
        self.context = replace(self.context, artifact_dir=self.root / 'other')
        result = self.run_process(subprocess.CompletedProcess(['claude'], 0, 'plain text', ''))
        self.assertEqual(result.status, 'completed')
        self.assertNotIn('claude_code_usage', result.artifacts)

    def test_timeout_and_failure_capture_complete_json(self):
        raw = SAMPLE.read_text()
        result = self.run_process(subprocess.TimeoutExpired(['claude'], 1, output=raw.encode()))
        self.assertEqual(result.status, 'failed')
        self.assertIn('claude_code_usage', result.artifacts)
        self.context = replace(self.context, artifact_dir=self.root / 'other')
        result = self.run_process(subprocess.CompletedProcess(['claude'], 1, raw, 'error'))
        self.assertEqual(result.status, 'failed')
        self.assertIn('claude_code_usage', result.artifacts)

    def test_start_crash_writes_no_usage(self):
        with patch('agent_taskflow.executors.claude_code.subprocess.run', side_effect=OSError('crash')):
            result = self.executor.run(self.context)
        self.assertEqual(result.status, 'blocked')
        self.assertNotIn('claude_code_usage', result.artifacts)

    def test_sums_and_unknowns(self):
        db = self.root / 'db.sqlite'
        with sqlite3.connect(db) as conn:
            conn.executescript('CREATE TABLE tasks(task_id TEXT, task_key TEXT); CREATE TABLE attempts(attempt_id TEXT, task_id TEXT, attempt_number INTEGER, artifact_root TEXT);')
            conn.execute('INSERT INTO tasks VALUES (?, ?)', ('t', 'AT-1'))
            for n in range(1, 4):
                root = self.root / str(n)
                root.mkdir()
                conn.execute('INSERT INTO attempts VALUES (?, ?, ?, ?)', (f'attempt-{n}', 't', n, str(root)))
                if n < 3:
                    self.context = replace(self.context, artifact_dir=root, attempt_id=f'attempt-{n}')
                    self.run_process(subprocess.CompletedProcess(['claude'], 0, SAMPLE.read_text(), ''))
                else:
                    (root / 'claude-code-usage.json').write_text('{}')
        summary = summarize_ticket_usage(db, 'AT-1')
        self.assertEqual((summary['attempts_total'], summary['attempts_with_usage'], summary['attempts_unknown']), (3, 2, 1))
        self.assertEqual(summary['input_tokens'], 4)
        self.assertAlmostEqual(summary['total_cost_usd'], 0.0922072)
        self.assertIsNone(summary['per_attempt'][2]['usage'])
