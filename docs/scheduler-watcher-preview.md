# Scheduler Watcher Preview

## Purpose

Level 8A adds a scheduler watcher dry-run / candidate preview. It previews
which mirrored tasks would be eligible for later operator-confirmed automation.
It does not execute tasks.

The preview follows the project boundary: manage work, not agents. It is a
read-only candidate surface for future one-shot / task-to-draft-PR automation,
not a scheduler loop and not execution authority.

## What It Exercises

- Local SQLite task mirror readback.
- Existing scheduler candidate discovery.
- Candidate filtering for queued, blocked, waiting-approval, completed, and
  no-action states.
- Dry-run preview summaries.
- Safety flags proving no execution or mutation happened.

## What It Does Not Do

- No task execution.
- No one-shot pipeline call.
- No task-to-draft-PR pipeline call.
- No approved_task_runner.
- No executor.
- No validators.
- No branch push.
- No draft PR.
- No approval / merge / cleanup.
- No scheduler loop.
- No background worker.
- No automatic task picking.
- No cron/webhook/polling.
- No API endpoint.
- No Mission Control action UI.

## Commands

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_watcher_preview.py \
  --db-path /absolute/path/to/state.db \
  --limit 10 \
  --pretty
```

Smoke:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_watcher_preview_smoke.py
```

## Safety Boundary

This is preview only and read-only. Suggested commands are inert text in the
JSON payload; no suggested command is executed by the preview.

No execution happens. No GitHub mutation happens. No branch push happens. No draft PR is created. No approval, merge, or cleanup happens. Human review remains required before any action.
