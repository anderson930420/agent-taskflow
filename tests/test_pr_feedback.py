import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step5_support import make_fixture
from agent_taskflow.github_pr_adapter import PrSnapshot
from agent_taskflow.pr_feedback import process_feedback
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_feedback import list_ticket_feedback
from agent_taskflow.ticket_prompt import write_ticket_implementation_prompt


class PrFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.store = TaskMirrorStore(self.fx.db_path)
        self.key = self.fx.create_ticket('feedback').task_key
        self.store.update_task_status(self.key, 'waiting_for_review', source='test')
        self.adapter = Mock()
        self.policy = SimpleNamespace(pr_feedback={'trigger': '@taskflow', 'max_ci_fix_attempts': 2})
        patcher = patch('agent_taskflow.pr_feedback.resolve_execution_policy', return_value=self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_feedback(self, snapshot, confirm=True):
        return process_feedback(self.store.get_task(self.key), snapshot, repo='owner/repo',
                                cwd=Path(self.fx.db_path).parent, store=self.store,
                                adapter=self.adapter, confirm=confirm)

    def snapshot(self, **kw):
        return PrSnapshot(number=1, state='open', head_sha='abc', **kw)

    def test_combined_comment_retry_and_prompt_and_deduplication(self):
        comments = ({'id': '1', 'body': '@Taskflow fix <this>', 'author': {'login': 'owner'}, 'url': 'https://example/1', 'createdAt': '2025-01-01'},
                    {'id': '2', 'body': '@taskflow also fix', 'author': {'login': 'owner'}, 'createdAt': '2025-01-02'},
                    {'id': '3', 'body': '@taskflow bad', 'author': {'login': 'other'}},
                    {'id': '4', 'body': '@taskflow <!-- taskflow:ack -->', 'author': {'login': 'owner'}})
        snap = self.snapshot(comments=comments)
        before = self.store.list_task_events(self.key)
        self.assertIn('pending', self.run_feedback(snap, False))
        self.assertEqual(before, self.store.list_task_events(self.key))
        self.assertIn('started', self.run_feedback(snap))
        self.assertEqual(self.store.get_task(self.key).status, 'created')
        self.assertEqual(self.adapter.comment_pr.call_count, 1)
        feedback = list_ticket_feedback(self.fx.db_path, self.key)
        self.assertEqual(len(feedback), 1)
        self.assertIn('fix <this>', feedback[0]['note'])
        self.assertNotIn('bad', feedback[0]['note'])
        self.assertLess(feedback[0]['note'].index('fix <this>'), feedback[0]['note'].index('also fix'))
        events = [json.loads(e.payload_json) for e in self.store.list_task_events(self.key) if e.event_type == 'pr_feedback_attempt']
        self.assertEqual(len(events[0]['triggers']), 2)
        path = write_ticket_implementation_prompt('original', Path(self.fx.db_path).parent / 'prompt', db_path=self.fx.db_path, task_key=self.key)
        self.assertIn('fix &lt;this&gt;', path.read_text())
        self.store.update_task_status(self.key, 'waiting_for_review', source='test')
        self.assertEqual(self.run_feedback(snap)['skipped'], 'no_pending_trigger')

    def test_ci_once_per_head_and_cap(self):
        check = ({'name': 'test', 'conclusion': 'FAILURE', 'detailsUrl': 'https://example/check'},)
        for sha in ('a', 'b'):
            result = self.run_feedback(PrSnapshot(number=1, state='open', head_sha=sha, checks=check))
            self.assertIn('started', result)
            self.store.update_task_status(self.key, 'waiting_for_review', source='test')
            self.assertEqual(self.run_feedback(PrSnapshot(number=1, state='open', head_sha=sha, checks=check))['skipped'], 'no_pending_trigger')
        self.assertEqual(self.run_feedback(PrSnapshot(number=1, state='open', head_sha='c', checks=check))['skipped'], 'no_pending_trigger')

    def test_review_ack_failure_and_replay(self):
        self.adapter.comment_pr.side_effect = RuntimeError('offline')
        review = {'id': 'review1', 'author': 'owner', 'body': '@taskflow please improve',
                  'url': 'https://example/review', 'submitted_at': '2025-01-01'}
        snapshot = self.snapshot(reviews=(review,), review_decision='changes_requested')
        self.assertIn('started', self.run_feedback(snapshot))
        self.store.update_task_status(self.key, 'waiting_for_review', source='test')
        self.assertTrue(self.run_feedback(snapshot)['handled_review'])
        self.assertEqual(len(list_ticket_feedback(self.fx.db_path, self.key)), 1)

    def test_closed_merged_and_other_status_do_nothing(self):
        self.assertIn('skipped', self.run_feedback(PrSnapshot(number=1, state='closed')))
        self.assertIn('skipped', self.run_feedback(PrSnapshot(number=1, state='open', merged=True)))
        self.store.update_task_status(self.key, 'paused', source='test')
        self.assertIn('skipped', self.run_feedback(self.snapshot()))


class PrFeedbackPolicyTests(unittest.TestCase):
    def test_policy_defaults_and_validation(self):
        from execution_policy_support import policy_block
        from agent_taskflow.execution_policy import ExecutionPolicyError, parse_execution_policy
        def parse(value):
            return parse_execution_policy('p', policy_block(pr_feedback=value), registry_path=Path('/tmp/registry'))
        policy = parse({})
        self.assertEqual(policy.pr_feedback, {'trigger': '@taskflow', 'max_ci_fix_attempts': 2})
        self.assertEqual(policy.canonical()['pr_feedback'], policy.pr_feedback)
        for invalid in (None, [], {'extra': True}, {'trigger': ''}, {'allowed_authors': []},
                        {'allowed_authors': ['bad/user']}, {'max_ci_fix_attempts': True},
                        {'max_ci_fix_attempts': -1}):
            with self.subTest(invalid=invalid), self.assertRaises(ExecutionPolicyError):
                parse(invalid)
