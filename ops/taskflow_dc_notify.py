#!/usr/bin/env python3
"""agent-taskflow -> Discord webhook notifier.

Deterministic, stdlib-only. Reads the taskflow SQLite mirror directly,
detects tasks that entered a notify-worthy status since the last run,
and posts one Discord message per transition with the draft PR link
when available.

Design rules (aligned with agent-taskflow principles):
- Read-only against the taskflow DB. Never writes to it.
- Own state lives in a separate JSON file (STATE_PATH).
- No LLM, no polling loop; run it from cron or append to the
  scheduler tick wrapper.

Usage:
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... \
        python3 taskflow_dc_notify.py

Cron example (every 5 min):
    */5 * * * * DISCORD_WEBHOOK_URL=... /usr/bin/python3 /opt/agent-taskflow/ops/taskflow_dc_notify.py >> /var/log/taskflow-notify.log 2>&1
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---- Config: adjust these three paths to your VPS layout -------------------
DB_PATH = Path(os.environ.get("TASKFLOW_DB_PATH", "/opt/agent-taskflow/data/taskflow.sqlite3"))
ARTIFACT_ROOT = Path(os.environ.get("TASKFLOW_ARTIFACT_ROOT", "/opt/agent-taskflow/artifacts"))
STATE_PATH = Path(os.environ.get("TASKFLOW_NOTIFY_STATE", "/opt/agent-taskflow/ops/notify_state.json"))

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Statuses that require Anderson's attention.
NOTIFY_STATUSES = {
    "waiting_approval": "🟡 等待批准",
    "waiting_for_review": "🟡 等待 review",
    "blocked": "🔴 Blocked",
}
# Terminal statuses worth a low-key confirmation message.
DONE_STATUSES = {
    "accepted": "✅ 已接受",
    "completed": "✅ 已完成",
    "rejected": "⛔ 已拒絕",
}


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def read_tasks() -> list[dict]:
    # Read-only URI so this script can never mutate the taskflow DB.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT task_key, project, title, status, updated_at FROM tasks"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def pr_url_for(task_key: str) -> str | None:
    candidate = ARTIFACT_ROOT / "draft_pr" / task_key / "draft_pr.json"
    if not candidate.exists():
        return None
    try:
        payload = json.loads(candidate.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    url = payload.get("pr_url") or payload.get("url")
    return str(url) if url else None


def post_discord(content: str) -> bool:
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "taskflow-notify/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 - notify path must never crash cron
        print(f"[notify] Discord post failed: {exc}", file=sys.stderr)
        return False


def format_message(task: dict, label: str, pr_url: str | None) -> str:
    lines = [
        f"{label}  **{task['task_key']}**",
        f"{task.get('project', '?')} — {task.get('title') or '(no title)'}",
    ]
    if pr_url:
        lines.append(f"PR: {pr_url}")
    lines.append(f"updated_at: {task.get('updated_at', '?')}")
    return "\n".join(lines)


def main() -> int:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        return 2
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    state = load_state()
    seen: dict = state.get("last_notified", {})  # task_key -> "status@updated_at"
    tasks = read_tasks()

    sent = 0
    for task in tasks:
        status = task["status"]
        label = NOTIFY_STATUSES.get(status) or DONE_STATUSES.get(status)
        if not label:
            continue
        fingerprint = f"{status}@{task['updated_at']}"
        if seen.get(task["task_key"]) == fingerprint:
            continue  # already notified for this exact transition
        pr_url = pr_url_for(task["task_key"])
        if post_discord(format_message(task, label, pr_url)):
            seen[task["task_key"]] = fingerprint
            sent += 1

    state["last_notified"] = seen
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"[notify] {datetime.now(timezone.utc).isoformat()} sent={sent} tasks={len(tasks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
