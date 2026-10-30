"""Turn authorized GitHub feedback into an audited same-Ticket retry."""
from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace

from agent_taskflow.execution_policy import ExecutionPolicyError, resolve_execution_policy
from agent_taskflow.models import utc_now_iso
from agent_taskflow.ticket_feedback import add_ticket_feedback
from agent_taskflow.ticket_retry import retry_ticket

LOG = logging.getLogger(__name__)
ACK = "<!-- taskflow:ack -->"
_FAILURES = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}


def process_feedback(task, snapshot, *, repo, cwd, store, adapter, confirm):
    """Return a report and atomically record one combined retry when confirmed."""
    if task.status != "waiting_for_review" or snapshot.state != "open" or snapshot.merged:
        return {"skipped": "not_open_needs_review"}
    try:
        policy = resolve_execution_policy(task.project).pr_feedback
    except ExecutionPolicyError as exc:
        return {"skipped": exc.reason_code}
    if policy is None:
        return {"skipped": "disabled"}
    trigger = policy["trigger"]
    allowed = {a.casefold() for a in policy.get("allowed_authors", [repo.split("/", 1)[0]])}
    events = [json.loads(e.payload_json or "{}") for e in store.list_task_events(task.task_key)
              if e.event_type == "pr_feedback_attempt"]
    handled = {(item["kind"], str(item["ref"])) for event in events for item in event.get("triggers", [])}
    ci_count = sum(any(item["kind"] == "ci_failure" for item in event.get("triggers", [])) for event in events)
    pending = []
    for kind, entries in (("comment", snapshot.comments), ("review", snapshot.reviews)):
        for entry in entries:
            author = entry.get("author") or ""
            if isinstance(author, dict):
                author = author.get("login") or ""
            body = entry.get("body") or ""
            ident = entry.get("id")
            if not ident or not isinstance(body, str) or not isinstance(author, str):
                continue
            if author.casefold() not in allowed or ACK in body or not re.search(re.escape(trigger), body, re.I):
                continue
            ref = f"{kind}:{ident}"
            if ("pr_comment", ref) in handled:
                continue
            date = entry.get("createdAt") or entry.get("submitted_at") or ""
            url = entry.get("url") or ""
            content = re.sub(re.escape(trigger), "", body, flags=re.I).strip()
            pending.append((date, {"kind": "pr_comment", "ref": ref}, f"GitHub feedback from {author} ({url}, {date}):\n{content}"))
    failures = [c for c in snapshot.checks if str(c.get("conclusion") or c.get("state") or "").upper() in _FAILURES]
    sha = snapshot.head_sha
    if failures and sha and ("ci_failure", sha) not in handled and ci_count < policy["max_ci_fix_attempts"]:
        lines = ["Failing checks for head " + sha + ":"]
        runs = set()
        for check in failures:
            url = check.get("detailsUrl") or check.get("targetUrl") or ""
            lines.append(f"- {check.get('name') or check.get('context') or 'check'}: {check.get('conclusion') or check.get('state')} {url}")
            match = re.search(r"/actions/runs/(\d+)", url)
            if match:
                runs.add(int(match.group(1)))
        if confirm:
            for run in sorted(runs):
                try:
                    log = adapter.failed_logs(run_id=run, cwd=cwd)
                    jobs = {}
                    for line in log.splitlines():
                        jobs.setdefault(line.split("\t", 1)[0], []).append(line)
                    for job, job_lines in jobs.items():
                        lines.append(f"Failed logs for {job} (run {run}):\n" + "\n".join(job_lines[-150:]))
                except Exception as exc:
                    LOG.warning("Failed to fetch CI logs for run %s: %s", run, exc)
        pending.append(("~", {"kind": "ci_failure", "ref": sha}, "\n".join(lines)))
    if not pending:
        latest = snapshot.latest_review or {}
        already_reviewed = ("pr_comment", f"review:{latest.get('id')}") in handled
        return {"skipped": "no_pending_trigger", "handled_review": already_reviewed}
    pending.sort(key=lambda item: item[0])
    refs = [item[1] for item in pending]
    report = {"task_key": task.task_key, "trigger": refs[0]["kind"], "ref": refs[0]["ref"], "triggers": refs}
    if not confirm:
        return {"pending": report}
    note = "\n\n".join(item[2] for item in pending)
    request = SimpleNamespace(task_key=task.task_key, from_status="waiting_for_review",
        reason="PR feedback", actor="integration_watcher", request_id=None,
        expected_old_attempt_id=None, expected_reset_generation=None, dry_run=False, confirm_reset=True)
    def record(conn):
        add_ticket_feedback(store.db_path, task.task_key, note, source=refs[0]["kind"], actor="integration_watcher", _conn=conn)
        row = conn.execute("SELECT COALESCE(MAX(attempt_number), 0) FROM attempts WHERE task_id = (SELECT task_id FROM tasks WHERE task_key = ?)", (task.task_key,)).fetchone()
        attempt = int(row[0]) + 1
        report["attempt"] = attempt
        conn.execute("INSERT INTO task_events(task_key, event_type, source, message, payload_json, created_at) VALUES (?, 'pr_feedback_attempt', 'integration_watcher', ?, ?, ?)", (task.task_key, "PR feedback started a new attempt", json.dumps({**report, "attempt_count": attempt}, sort_keys=True), utc_now_iso()))
    retry_ticket(request, store=store, in_transaction=record, pr_feedback=True)
    try:
        adapter.comment_pr(pr_number=snapshot.number, cwd=cwd, body=f"Taskflow: starting a new attempt for this feedback ({task.task_key}, attempt {report['attempt']}).\n\n{ACK}")
    except Exception as exc:
        LOG.warning("PR feedback acknowledgement failed for %s: %s", task.task_key, exc)
    return {"started": report}
