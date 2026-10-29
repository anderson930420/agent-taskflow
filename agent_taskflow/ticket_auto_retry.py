"""Bounded, audited retries of transient Ticket failures."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable

from agent_taskflow.lifecycle_control import _effective_runtime_control_in_connection
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.task_status_reset import TaskStatusResetRequest
from agent_taskflow.ticket_dependencies import unreleased_dependency_in_connection
from agent_taskflow.ticket_lifecycle import tasks_has_ticket_columns
from agent_taskflow.ticket_retry import TicketRetryError, retry_ticket

MAX_AUTO_RETRIES = 2
BASE_DELAY_SECONDS = 300
BACKOFF_FACTOR = 4
_SOURCE = "ticket_auto_retry"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def maintain_auto_retries(db_path: str | Path, *, clock: Callable[[], datetime] | None = None) -> dict:
    """Retry due failures; return retried, waiting, and exhausted Ticket keys."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ValueError('clock must return a timezone-aware datetime')
    now = now.astimezone(timezone.utc)
    result: dict = {"retried": [], "waiting": [], "exhausted": []}
    store = TaskMirrorStore(db_path)
    with closing(connect(db_path)) as conn:
        if not tasks_has_ticket_columns(conn):
            return result
        keys = [r[0] for r in conn.execute("SELECT task_key FROM tasks WHERE prompt IS NOT NULL AND status = 'failed' ORDER BY task_key")]

    for key in keys:
        with closing(connect(db_path)) as conn:
            task = conn.execute("SELECT task_id, status FROM tasks WHERE task_key = ?", (key,)).fetchone()
            if not task or task['status'] != 'failed':
                continue
            events = conn.execute("SELECT * FROM task_events WHERE task_key = ? ORDER BY id", (key,)).fetchall()
            statuses = [e for e in events if e['event_type'] == 'status_changed']
            if not statuses:
                continue
            failure = statuses[-1]
            try:
                if json.loads(failure['payload_json'] or '{}').get('status') != 'failed':
                    continue
                failed_at = _parse_time(failure['created_at'])
            except (ValueError, TypeError, AttributeError):
                continue
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            attempt = None
            if 'attempts' in tables:
                attempt = conn.execute("SELECT attempt_id FROM attempts WHERE task_id = ? ORDER BY attempt_number DESC LIMIT 1", (task['task_id'],)).fetchone()
            attempt_id = attempt['attempt_id'] if attempt else None
            if _effective_runtime_control_in_connection(conn, task_key=key, attempt_id=attempt_id).mode != 'running':
                continue
            if unreleased_dependency_in_connection(conn, key):
                continue
            if attempt_id and 'lifecycle_events' in tables and conn.execute("SELECT 1 FROM lifecycle_events WHERE attempt_id = ? AND reason_code = 'operator_kill_requested' LIMIT 1", (attempt_id,)).fetchone():
                continue
            if attempt_id and 'executor_processes' in tables and conn.execute("SELECT 1 FROM executor_processes WHERE attempt_id = ? AND termination_reason = 'operator_kill_requested' LIMIT 1", (attempt_id,)).fetchone():
                continue
            count = sum(e['event_type'] == 'auto_retry' for e in events)
            if count >= MAX_AUTO_RETRIES:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute("SELECT 1 FROM tasks WHERE task_key = ? AND status = 'failed'", (key,)).fetchone() and not conn.execute("SELECT 1 FROM task_events WHERE task_key = ? AND event_type = 'auto_retry_exhausted'", (key,)).fetchone():
                    conn.execute("INSERT INTO task_events(task_key,event_type,source,message,payload_json,created_at) VALUES (?, 'auto_retry_exhausted', 'ticket_auto_retry', 'Automatic retries exhausted', ?, ?)", (key, json.dumps({'max': MAX_AUTO_RETRIES}), now.isoformat()))
                conn.commit()
                result['exhausted'].append(key)
                continue
            delay = BASE_DELAY_SECONDS * BACKOFF_FACTOR ** count
            retry_at = failed_at + timedelta(seconds=delay)
            if now < retry_at:
                result['waiting'].append({'task_key': key, 'retry_at': retry_at.isoformat()})
                continue
            payload = {'retry_number': count + 1, 'max': MAX_AUTO_RETRIES, 'delay_seconds': delay, 'failed_at': failure['created_at'], 'reason': 'infrastructure failure'}
            failure_id = failure['id']

        def record_retry(conn: sqlite3.Connection) -> None:
            latest = conn.execute("SELECT id FROM task_events WHERE task_key = ? AND event_type = 'status_changed' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            retry_count = conn.execute("SELECT count(*) FROM task_events WHERE task_key = ? AND event_type = 'auto_retry'", (key,)).fetchone()[0]
            if latest is None or latest['id'] != failure_id or retry_count != count:
                raise TicketRetryError('automatic retry snapshot changed')
            store.record_task_event(key, 'auto_retry', _SOURCE, message='Automatic Ticket retry', payload=payload, _conn=conn)

        request = TaskStatusResetRequest(task_key=key, from_status='failed', reason='automatic retry after infrastructure failure', confirm_reset=True, actor=_SOURCE, expected_old_attempt_id=attempt_id)
        try:
            retry = retry_ticket(request, store=store, in_transaction=record_retry)
        except (TicketRetryError, sqlite3.IntegrityError):
            continue
        if retry['mutated']:
            result['retried'].append(key)
    return result
