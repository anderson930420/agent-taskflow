# GitHub Issue One-Task Real Cron Profile (Level 10H)

This is a cautious cron profile for running the confirmed, real `opencode`
execution-only scheduler tick on a fixed interval. It wraps
`scripts/run_github_issue_one_task_scheduler_tick.py` with the
`deploy/cron/github-issue-one-task-real-opencode.cron.example` entry and adds
no new behavior beyond scheduling the already-proven execution-only path.

See also `docs/github-issue-one-task-scheduler-tick.md` for the underlying tick
contract.

## Prerequisite: Level 10G full pass

Do not install this profile until the Level 10G real executor smoke has fully
passed at least once, meaning:

- a GitHub Issue was ingested into a local task mirror,
- `implementation_prompt.md` was generated,
- the real `opencode` executor ran with model
  `minimax-coding-plan/MiniMax-M2.7`,
- the executor completed successfully (`exit_code=0`),
- the policy validator passed,
- the task reached `waiting_approval`,
- publication was skipped, and
- no branch push, no draft PR, no merge, no approval, and no cleanup occurred.

The cron profile only schedules that same execution-only path. It does not make
it safer or more capable; it just runs it on a timer.

## What this cron profile does

- Runs every 30 minutes by default (`*/30 * * * *`).
- Changes into `/home/ubuntu/agent-taskflow` and sources
  `.venv/bin/activate` if present.
- Runs the confirmed scheduler tick with the real `opencode` executor and the
  policy validator.
- Processes at most one eligible GitHub Issue / one local task per tick and
  drives it to `waiting_approval`, then stops (execution-only).
- Relies on the shared non-overlap lock so a scheduled tick can never overlap a
  manual write-capable run or another tick. A contended tick returns a safe
  no-op result and stops.
- Appends JSON results to
  `/home/ubuntu/agent-taskflow/logs/github-issue-one-task-real-opencode.jsonl`.

## What this cron profile does not do

- It does not enable cron automatically or modify your actual crontab.
- It does not add a daemon, scheduler loop, webhook, background worker, or
  multi-task batch.
- It does not pass `--publish-after-execution`, so it stays execution-only.
- It performs no auto-approval and no auto-merge.
- It performs no branch push and no draft PR creation.
- It performs no cleanup automation and deletes no branches or worktrees.

Human review and human merge remain the final gates.

## Required environment / auth

API credentials must be provided through the environment, never committed in
the cron file:

- `gh auth` must work for the configured repository
  (`anderson930420/agent-taskflow`). Verify with `gh auth status`.
- `opencode` auth must work for the configured provider. Verify that the
  `opencode` CLI can run a real model call.
- The model string uses the `provider/model` format, here
  `minimax-coding-plan/MiniMax-M2.7`.

## How to install

This profile is install-by-hand only. Back up the current crontab, append the
example line manually, then reload it:

```bash
crontab -l > /tmp/agent-taskflow.cron.bak
# Append the example crontab line manually, for example by editing
# /tmp/agent-taskflow.cron (starting from the backup) and copying the line
# from deploy/cron/github-issue-one-task-real-opencode.cron.example.
crontab /tmp/agent-taskflow.cron
```

Ensure the log directory exists first:

```bash
mkdir -p /home/ubuntu/agent-taskflow/logs
```

## How to verify

After installation, watch the JSON log and confirm the safe shape:

```bash
tail -f /home/ubuntu/agent-taskflow/logs/github-issue-one-task-real-opencode.jsonl
```

For each tick check:

- `selected_task_key` — the ingested task key when an issue was selected.
- `status` — either `execution_completed` (a task reached `waiting_approval`)
  or `no_eligible_issues` (nothing to do this tick). A contended tick reports
  `status=locked`.
- Inspect the `TaskMirrorStore` to confirm the selected task is in
  `waiting_approval` and that no branch push, draft PR, approval, merge, or
  cleanup happened.

## How to disable

Comment out or remove the crontab line:

```bash
crontab -e
# Comment the line with a leading `#`, or delete it entirely.
```

## How to handle failures

- Blocked tasks are visible through the scheduler watcher preview and the
  waiting-approval summary; review them before acting.
- A failed-ingestion registry exists in the store, and discovery skips records
  that are in active backoff or quarantined, so a transient ingestion failure
  does not wedge the tick.
- Do not blindly rerun the same blocked task. Investigate the blocker, fix the
  root cause, and only then allow the task to be retried.

## Rollout recommendation

- Start at every 30 minutes.
- Observe for 24 hours.
- Only then consider a higher frequency or additional validators.

## Safety boundaries

- execution-only
- no auto PR
- no publish
- no auto-merge
- no auto-approval
- no cleanup
- no branch or worktree deletion
- human review remains the final gate
