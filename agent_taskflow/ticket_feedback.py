"""Auditable owner feedback shared by Ticket actions."""
from __future__ import annotations

import json
import re
from pathlib import Path
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore


def validate_note(note: str) -> str:
    if not isinstance(note, str) or not note.strip():
        raise ValueError("note must not be empty")
    if len(note) > 4000:
        raise ValueError("note must be at most 4000 characters")
    return note


def add_ticket_feedback(db_path: str | Path, task_key: str, note: str, *, source: str, actor: str = "mission_control", extra: dict | None = None, _conn=None) -> None:
    validate_note(note)
    if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", source):
        raise ValueError("source must be a short lowercase token")
    payload = {**(extra or {}), "note": note, "source": source}
    if _conn is not None:
        _conn.execute("INSERT INTO task_events(task_key, event_type, source, message, payload_json, created_at) VALUES (?, 'owner_feedback', ?, ?, ?, ?)", (task_key, actor, "Owner feedback", json.dumps(payload, sort_keys=True), utc_now_iso()))
    else:
        store = TaskMirrorStore(db_path)
        if store.get_task(task_key) is None:
            raise KeyError(f"Task not found: {task_key}")
        store.record_task_event(task_key, "owner_feedback", actor, message="Owner feedback", payload=payload)


def list_ticket_feedback(db_path: str | Path, task_key: str) -> list[dict]:
    result = []
    for event in TaskMirrorStore(db_path).list_task_events(task_key):
        if event.event_type == "owner_feedback":
            payload = json.loads(event.payload_json or "{}")
            result.append({**payload, "created_at": event.created_at})
    return result
