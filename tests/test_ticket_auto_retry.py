from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step5_support import make_fixture
from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.ticket_auto_retry import maintain_auto_retries
from agent_taskflow.ticket_retry import retry_ticket
from agent_taskflow.parallel_scheduler import run_scheduler_tick


class AutoRetryTests(unittest.TestCase):
    def setUp(self):
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.store = TaskMirrorStore(self.fx.db_path)
        self.key = self.fx.create_ticket('retry').task_key
        with connect(self.fx.db_path) as conn:
            self.initial_triggers = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")]

    def fail_ticket(self, key=None, status='failed'):
        key = key or self.key
        self.store.update_task_status(key, status, source='test', message='failure')
        event = [e for e in self.store.list_task_events(key) if e.event_type == 'status_changed'][-1]
        return datetime.fromisoformat(event.created_at.replace('Z', '+00:00'))

    def maintain(self, now):
        return maintain_auto_retries(self.fx.db_path, clock=lambda: now)

    def test_delays_exhaustion_and_audit(self):
        failed = self.fail_ticket()
        self.assertEqual(self.maintain(failed + timedelta(seconds=299))['waiting'][0]['task_key'], self.key)
        self.assertEqual(self.maintain(failed + timedelta(seconds=300))['retried'], [self.key])
        self.assertEqual(self.fx.status(self.key), 'created')
        self.assertEqual(self.maintain(failed + timedelta(days=1))['retried'], [])
        failed = self.fail_ticket()
        self.assertEqual(self.maintain(failed + timedelta(seconds=1199))['waiting'][0]['task_key'], self.key)
        self.assertEqual(self.maintain(failed + timedelta(seconds=1200))['retried'], [self.key])
        failed = self.fail_ticket()
        self.assertEqual(self.maintain(failed)['exhausted'], [self.key])
        self.maintain(failed)
        events = self.store.list_task_events(self.key)
        retries = [json.loads(e.payload_json) for e in events if e.event_type == 'auto_retry']
        self.assertEqual([x['retry_number'] for x in retries], [1, 2])
        self.assertEqual([x['delay_seconds'] for x in retries], [300, 1200])
        self.assertEqual(len([e for e in events if e.event_type == 'auto_retry_exhausted']), 1)
        self.assertEqual(len([e for e in events if e.event_type == 'note' and 'ticket_retry_reset' in (e.payload_json or '')]), 2)
        self.assertEqual([e.source for e in events if e.event_type == 'status_changed' and json.loads(e.payload_json or '{}').get('status') == 'created'], ['ticket_auto_retry', 'ticket_auto_retry'])
        self.assertEqual(set(retries[0]), {'retry_number', 'max', 'delay_seconds', 'failed_at', 'reason'})
        with connect(self.fx.db_path) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")], self.initial_triggers)
            self.assertNotIn('ticket_auto_retry_audit', self.initial_triggers)
        self.assertEqual(self.fx.status(self.key), 'failed')

    def test_changed_snapshot_aborts_without_retry_writes(self):
        for event_type in ('status_changed', 'auto_retry'):
            with self.subTest(event_type=event_type):
                key = self.fx.create_ticket('snapshot-' + event_type).task_key
                failed = self.fail_ticket(key)
                before_retry = []

                def change_snapshot(request, **kwargs):
                    if request.task_key != key:
                        return retry_ticket(request, **kwargs)
                    self.store.record_task_event(key, event_type, 'test', payload={'status': 'failed'})
                    before_retry.extend(self.store.list_task_events(key))
                    return retry_ticket(request, **kwargs)

                with mock.patch('agent_taskflow.ticket_auto_retry.retry_ticket', side_effect=change_snapshot):
                    self.assertNotIn(key, self.maintain(failed + timedelta(days=1))['retried'])
                self.assertEqual(self.fx.status(key), 'failed')
                self.assertEqual(self.store.list_task_events(key), before_retry)

    def test_paused_and_needs_decision(self):
        failed = self.fail_ticket()
        RuntimeControlStore(self.fx.db_path).pause(scope_kind='task', scope_id=self.key, actor='test')
        self.assertEqual(self.maintain(failed + timedelta(days=1))['retried'], [])
        self.store.update_task_status(self.key, 'needs_decision', source='test')
        self.assertEqual(self.maintain(failed + timedelta(days=1))['retried'], [])

    def test_unreleased_dependency(self):
        blocker = self.fx.create_ticket('blocker').task_key
        blocked = self.fx.create_ticket('blocked', blocked_by=blocker).task_key
        failed = self.fail_ticket(blocked)
        self.assertNotIn(blocked, self.maintain(failed + timedelta(days=1))['retried'])

    def test_operator_kill_evidence(self):
        from agent_taskflow.runtime_admission import RuntimeAdmissionStore
        claim = RuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id='test', ttl_seconds=600)
        # The cooperative kill's lifecycle reason is durable even after clearing controls.
        with connect(self.fx.db_path) as conn:
            conn.execute("INSERT INTO lifecycle_events(task_id,attempt_id,from_status,to_status,reason_code,actor,timestamp,metadata_json) VALUES (?, ?, 'implementing', 'execution_aborted', 'operator_kill_requested', 'test', ?, '{}')", (claim.task_id, claim.attempt_id, datetime.now(timezone.utc).isoformat()))
        failed = self.fail_ticket()
        with mock.patch('agent_taskflow.ticket_auto_retry.retry_ticket') as retry:
            self.assertNotIn(self.key, self.maintain(failed + timedelta(days=1))['retried'])
            retry.assert_not_called()

    def test_tick_json_section(self):
        self.fail_ticket()
        result = run_scheduler_tick(self.fx.db_path)
        self.assertEqual(set(result.to_dict()['auto_retry']), {'retried', 'waiting', 'exhausted'})
        self.assertEqual(result.to_dict()['auto_retry']['waiting'][0]['task_key'], self.key)
