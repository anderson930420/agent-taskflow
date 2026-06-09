# Active Cron Observability Rollout (P4-j)

This is the **active cron observability rollout** runbook. It is a
**documentation only** / **runbook only** procedure for safely updating the
*active* real `opencode` cron line so it passes `--include-observability-summary`.

> **This phase does not modify the active crontab.** It does not run
> `crontab -e`, `crontab <file>`, or otherwise install, replace, or mutate any
> cron entry. Every command in this runbook is either read-only inspection,
> local file preparation, or an explicitly human-gated step that the operator
> must run manually after review. Applying the change to the live schedule is a
> **separate explicit human operator action**, not something this phase
> performs.

See also:

- `docs/github-issue-one-task-real-cron-profile.md` — the cautious Level 10H
  real cron profile this rollout targets.
- `docs/real-scheduled-execution-observability.md` — the P4-h read-only
  dashboard / summarizer that consumes the rolled-out summary.

## Purpose

The active real `opencode` cron line currently runs the confirmed scheduler
tick but does **not** pass `--include-observability-summary`. Rolling that flag
out lets us:

- safely roll out `--include-observability-summary` to the active real
  `opencode` cron line;
- make future JSONL scheduler tick lines additionally carry a top-level
  `observability_summary` (the normalized `UnifiedExecutionSummary`,
  `schema_version=execution_observability_summary.v1`);
- allow the P4-h dashboard / summarizer
  (`scripts/summarize_real_scheduled_execution.py`) to read the unified
  `observability_summary` when present;
- preserve the **legacy fallback** so old log lines that predate the rollout
  (lines without `observability_summary`) remain fully readable — the
  summarizer keeps reading the legacy scheduler tick payload exactly as before.

Adding the flag is an additive observability change. It does **not** change
scheduler execution behavior, one-task automation, the approved task runner,
executors, validators, the database schema, or Mission Control.

## Scope

- This runbook is **documentation only** / **runbook only**.
- It **does not modify the active crontab** in this phase.
- It does not run `crontab -e`, `crontab <file>`, or any command that installs,
  replaces, or mutates a live cron entry.
- Any actual crontab update is a **separate explicit human operator action**
  performed manually after review (see "Explicit human-gated apply step").
- The **scheduler tick is not migrated to ExecutionEngine** by this rollout, and
  scheduler execution semantics are unchanged.

## Preconditions

Confirm all of the following before starting:

- `main` is up to date (the rollout is documented against current `main`).
- The P4-i cron **example**
  (`deploy/cron/github-issue-one-task-real-opencode.cron.example`) is merged and
  already includes `--include-observability-summary`.
- The active cron currently points at the preserved runtime worktree
  `/home/ubuntu/agent-taskflow-cron`.
- The runtime worktree is preserved (this rollout deletes no worktree).
- The log path remains
  `/home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl`.

## Step 1 — Read-only inspection

None of these commands change anything; they only read the current state.

Show the current active crontab:

```bash
crontab -l
```

Grep the current real `opencode` cron line out of the active crontab:

```bash
crontab -l | grep run_github_issue_one_task_scheduler_tick.py
```

Compare the active cron line to the committed P4-i cron example to see exactly
what differs (the active line should differ only by the missing
`--include-observability-summary` flag):

```bash
diff \
  <(crontab -l | grep run_github_issue_one_task_scheduler_tick.py) \
  <(grep run_github_issue_one_task_scheduler_tick.py \
      deploy/cron/github-issue-one-task-real-opencode.cron.example)
```

Read the current read-only execution summary (this is the P4-h dashboard /
summarizer; it modifies nothing):

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_real_scheduled_execution.py \
  --db-path /home/ubuntu/.agent-taskflow/state.db \
  --log-path /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl
```

Run the local workspace inventory to confirm the runtime worktree state before
any change (read-only):

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_local_workspace_inventory.py
```

## Step 2 — Safe preparation (local files only)

These steps prepare candidate files on disk. They **do not** install anything;
no command here calls `crontab -e` or `crontab <file>`.

Save the current crontab to a timestamped **backup crontab** file:

```bash
crontab -l > "/home/ubuntu/agent-taskflow.cron.backup.$(date +%Y%m%d-%H%M%S)"
```

Create a **candidate crontab** file starting from the backup:

```bash
crontab -l > /home/ubuntu/agent-taskflow.cron.candidate
```

Edit the candidate **only** to add `--include-observability-summary` to the real
`opencode` scheduler tick line. The flag belongs on the actual command line,
immediately before `--json`, matching the committed cron example. Do **not**
change anything else on the line (repo, db path, worktree root, log path, the
`*/30 * * * *` schedule, etc.).

Inspect the diff between the backup and the candidate to confirm the change:

```bash
diff /home/ubuntu/agent-taskflow.cron.backup.<timestamp> \
     /home/ubuntu/agent-taskflow.cron.candidate
```

Confirm that the **only intended semantic change is adding
`--include-observability-summary`** to the real `opencode` cron line. If the
diff shows anything else, stop and re-create the candidate from the backup.

## Step 3 — Explicit human-gated apply step (NOT executed by this phase)

> **Manual operator action only. This phase does not run the command below.**

After reviewing the diff, the operator — and only the operator, by hand —
applies the candidate to the live schedule:

```bash
crontab /path/to/candidate
```

For example, `crontab /home/ubuntu/agent-taskflow.cron.candidate`. This runbook
intentionally does not execute this command; it merely shows it. The operator
must run it manually only after review.

## Step 4 — Post-rollout verification

After the operator has applied the candidate:

- Wait for the next scheduled cron tick. Do **not** trigger an extra manual tick
  as part of this runbook; a manual confirmed tick is only acceptable if it is
  separately approved as its own action.
- Tail the log safely (read-only):

  ```bash
  tail -n 5 /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl
  ```

- Confirm the newest JSONL line contains a top-level `observability_summary`:

  ```bash
  tail -n 1 /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl \
    | python3 -m json.tool | grep observability_summary
  ```

- Run the read-only summarizer again:

  ```bash
  PYTHONPATH=. .venv/bin/python scripts/summarize_real_scheduled_execution.py \
    --db-path /home/ubuntu/.agent-taskflow/state.db \
    --log-path /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl \
    --json
  ```

- Verify that, once a summary-bearing line is present, the summarizer reports
  `last_tick_uses_observability_summary: true`.
- Verify the **legacy fallback** still works: older log lines that predate the
  rollout (lines without `observability_summary`) remain readable, and a tick
  with no summary still reports `last_tick_uses_observability_summary: false`.
- Verify there are no failures, no lock contention, no GitHub mutation, no PR,
  no merge, and no cleanup as a result of the rollout — the tick stays the same
  execution-only path it was before, now with additive observability output.

## Step 5 — Rollback

If anything looks wrong, the operator restores the saved **backup crontab**
manually:

```bash
crontab /path/to/backup
```

For example, `crontab /home/ubuntu/agent-taskflow.cron.backup.<timestamp>`.
Then verify the rollback:

- Confirm the active cron line no longer includes
  `--include-observability-summary`:

  ```bash
  crontab -l | grep run_github_issue_one_task_scheduler_tick.py
  ```

- The dashboard / summarizer should still work and fall back to the **legacy
  fallback** for legacy payloads (lines without `observability_summary`).

## Safety boundaries

- **No active crontab modification in this PR / phase.** This runbook is
  documentation only; the active crontab is not modified by this phase.
- No scheduler execution behavior change.
- No change to one-task automation, the approved task runner, executors,
  validators, or the database schema.
- The **scheduler tick is not migrated to ExecutionEngine**.
- No ExecutionEngine migration.
- No Mission Control change and no README change.
- This phase performs **no approval**, **no merge**, **no cleanup**, **no
  archive**, **no closeout**, **no PR publication**, **no issue close**, **no
  branch deletion**, **no worktree deletion**, and **no GitHub mutation**.
- This phase adds **no daemon**, **no webhook**, **no background worker**, **no
  scheduler loop**, and **no multi-task behavior**.
