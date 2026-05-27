# Scheduler Watcher One-Task

## Purpose

Level 8B is a one-task-at-a-time confirmed watcher. It scans scheduler
watcher preview candidates, selects exactly one candidate using an
explicit selection mode, runs the existing task-to-draft-PR pipeline
once for that single candidate, and stops.

It is a one-shot watcher command, not a background scheduler, not a
scheduler loop, not a cron job, not a webhook, and not a poller. It
processes at most one task per invocation and never silently picks a
task automatically.

It follows the project boundary: manage work, not agents. Confirmation
flags and explicit selection remain required before any GitHub mutation
can happen.

## What It Exercises

- Level 8A scheduler watcher preview (read-only candidate surface).
- Explicit `task_key` selection or confirmed first-candidate selection.
- Level 7D task-to-draft-PR pipeline (one-shot pipeline plus PR
  preparation pipeline) for the one selected task.
- Idempotent resume via `--resume-existing` and `--resume-pr-preparation`
  passed through to the task-to-draft-PR pipeline.
- Smoke-grade fake `approved_task_runner`, fake branch push, and fake
  draft PR functions so the chain can be exercised without touching
  GitHub.

## Selection Policy

The watcher selects exactly one candidate per invocation. Allowed
selection modes:

1. Explicit `task_key`:
   - `--task-key AT-XYZ`
   - Selected candidate must match the supplied `task_key` and must
     either be eligible in preview or be a previously processed task
     that the operator is explicitly resuming with `--resume-existing` /
     `--resume-pr-preparation` flags.

2. First-candidate mode:
   - `--select-first-candidate` plus `--confirm-select-first-candidate`
   - Selects `candidates[0]` from preview only.
   - Never selects more than one candidate.
   - Requires the extra confirmation flag so picking the first eligible
     candidate is never silent or automatic.

Rules:

- Never more than one task is selected.
- If neither explicit `task_key` nor confirmed first-candidate mode is
  supplied, the watcher fails with `selection_required`.
- If both selection modes are supplied, the watcher fails with
  `ambiguous_selection_mode`.
- If first-candidate mode is supplied but
  `--confirm-select-first-candidate` is missing, the watcher fails with
  `first_candidate_selection_not_confirmed`.
- No silent automatic picking is allowed.

## Required Confirmation Flags

Confirmed mode requires every confirmation flag below. Any missing flag
causes the watcher to fail before any GitHub mutation:

- `--confirm-run-watcher-one-task`
- `--confirm-run-one-shot-pipeline`
- `--confirm-prepare-pr`
- `--confirm-github-mutations`
- `--confirm-branch-push`
- `--confirm-draft-pr`

## What It Does Not Do

- no background worker
- no scheduler loop
- no scheduler daemon
- no cron / no webhook / no polling
- no multi-task batch execution
- no automatic task picking
- no silent automatic picking
- no approval
- no merge
- no cleanup
- no task closeout
- no branch deletion
- no worktree deletion
- no Mission Control action UI
- no API endpoint
- no POST/PATCH/DELETE route

## Commands

Dry-run preview (default; safe, read-only):

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_watcher_one_task.py \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --pretty
```

Confirmed explicit task:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_watcher_one_task.py \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --task-key AT-EXAMPLE \
  --resume-existing \
  --resume-pr-preparation \
  --confirm-run-watcher-one-task \
  --confirm-run-one-shot-pipeline \
  --confirm-prepare-pr \
  --confirm-github-mutations \
  --confirm-branch-push \
  --confirm-draft-pr
```

Confirmed first candidate:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_watcher_one_task.py \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --select-first-candidate \
  --confirm-select-first-candidate \
  --resume-existing \
  --resume-pr-preparation \
  --confirm-run-watcher-one-task \
  --confirm-run-one-shot-pipeline \
  --confirm-prepare-pr \
  --confirm-github-mutations \
  --confirm-branch-push \
  --confirm-draft-pr
```

Smoke:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_watcher_one_task_smoke.py
```

## Safety Boundary

This is a one-task-at-a-time confirmed watcher only:

- one task per invocation
- preview is read-only
- confirmed mode executes at most one selected candidate
- first-candidate mode requires `--confirm-select-first-candidate`
- no silent automatic picking
- no background loop
- no scheduler daemon
- no cron / no webhook / no polling
- no multi-task batch execution
- no approval / no merge / no cleanup
- no task closeout
- no branch deletion / no worktree deletion
- no Mission Control action UI
- no API endpoint

Human final review remains required after the draft PR is opened by
the underlying task-to-draft-PR pipeline.
