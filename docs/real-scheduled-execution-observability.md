# Real Scheduled Execution Observability

`scripts/summarize_real_scheduled_execution.py` is a **read-only** operator
command that summarizes the real, cron-driven GitHub Issue one-task scheduler
tick (Level 10H). It reads an existing JSONL scheduler tick log and the local
task mirror and prints a review-oriented summary.

This tool adds **no automation capability**. It does not modify crontab, enable
or disable cron, call GitHub discovery, ingest issues, run an executor, run a
validator, publish, push, create a PR, merge, approve, clean up, delete a branch
or worktree, or start a daemon, scheduler loop, webhook, or background worker.
It only parses an append-only log and reads existing local state.

## Prerequisite: Level 10H cron installed

This command observes the output of the Level 10H cautious real scheduled tick.
That tick must already be installed and writing a JSONL log (one JSON tick
payload per line), for example from a clean runtime worktree such as
`/home/ubuntu/agent-taskflow-cron`. This tool does not install, enable, modify,
or disable that cron. If the log file does not exist yet, the command still
succeeds and reports a warning that the log was not found.

## What the command shows

- **Last tick**: `mode`, `status`, `ok`, `selected_task_key`, the selected issue
  (`number`, `title`, `url`), the `runner_config`
  (`executor`, `model`, `validators`, `worktree_root`), the `publication_config`
  (`publish_after_execution`, `mode`), the lock state
  (`acquired`, `contended`, `released`), and the tick's own safety flags.
- **Recent ticks**: counts over the most recent `--recent-limit` ticks —
  `total_parsed`, `ok_count`, `failure_count`, `no_eligible_count`,
  `execution_completed_count`, `lock_contention_count`, and the number of
  malformed log lines skipped.
- **Backlog**: `waiting_approval_count`, `blocked_count`, `queued_count`, plus
  recent waiting-approval and blocked task keys/titles (with blocked reasons).
- **Ingestion failure registry**: `ingestion_failure_count`,
  `quarantined_ingestion_failure_count`, and recent failure records.

## How to run JSON mode

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_real_scheduled_execution.py \
  --db-path /home/ubuntu/.agent-taskflow/state.db \
  --log-path /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl \
  --recent-limit 20 \
  --json
```

The JSON output includes `ok`, `schema_version`, `source`, `log_path`,
`db_path`, `last_tick`, `last_tick_observability_summary`,
`last_tick_uses_observability_summary`, `recent_ticks`, `backlog`,
`ingestion_failure_registry`, `warnings`, and `safety`.

## P4-h: reading the unified execution summary

P4-h lets the real scheduled execution dashboard / summarizer read a normalized
`UnifiedExecutionSummary` when it is present in a scheduler tick log line, while
preserving the legacy fallback.

The scheduler tick can optionally embed a JSON-safe `observability_summary`
object on each tick line when the cron tick is run with
`--include-observability-summary` (added in P4-g). When a parsed scheduler tick
log line carries a valid `observability_summary` (a mapping whose
`schema_version` is `execution_observability_summary.v1`), the summarizer:

- treats it as the normalized execution summary for that tick;
- exposes it verbatim under the stable key
  `last_tick_observability_summary` for the latest tick;
- sets `last_tick_uses_observability_summary` to `true`;
- reads dashboard-level normalized fields from it — `source`, `schema_version`,
  `ok`, `status`, `task_key`, `profile.executor`, `profile.model`,
  `profile.validators`, `publication_mode`, and `safety`;
- uses the unified summary `status` for the recent-tick status counts.

### Legacy fallback is preserved

This change is read-only and behavior-preserving:

- **Existing scheduler tick logs without `observability_summary` still work.**
  The summarizer falls back to the legacy scheduler tick payload exactly as
  before, the legacy `last_tick` fields are unchanged, and
  `last_tick_uses_observability_summary` is `false`.
- When an `observability_summary` is present but malformed (not a mapping, or
  carrying the wrong `schema_version`), the summarizer does not crash. It
  ignores the malformed summary, records a `malformed observability_summary`
  warning, falls back to the legacy tick payload, and still counts the tick as
  parsed when the tick payload itself is valid.
- Recent-tick counts (`ok_count`, `failure_count`, `no_eligible_count`,
  `execution_completed_count`, `lock_contention_count`, `malformed_line_count`,
  and `statuses`) keep their existing meaning. For logs with a valid
  `observability_summary`, the tick status is read from the unified summary;
  otherwise the legacy `status` field is used.

### What P4-h does not do

P4-h only changes the output reader / summarizer. It does **not** change cron and
makes **no cron change**: the live cron command is unchanged and still does not
pass `--include-observability-summary`. It does **not** migrate the scheduler
tick to ExecutionEngine — the **scheduler tick is not migrated to
ExecutionEngine** — and it does not change execution semantics, scheduler
execution behavior, one-task automation, the approved task runner, executors,
validators, or the database schema.

It performs no governance or GitHub side effects: **no approval**, **no merge**,
**no cleanup**, **no archive**, **no closeout**, **no PR publication**, **no
issue close**, **no branch deletion**, **no worktree deletion**, and **no GitHub
mutation**. It starts no daemon, webhook, background worker, or scheduler loop.

A future phase may enable the cron command to include
`--include-observability-summary`, but that is explicitly **not** done here.

## How to run human-readable mode

Omit `--json` for the default human-readable output:

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_real_scheduled_execution.py \
  --db-path /home/ubuntu/.agent-taskflow/state.db \
  --log-path /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl
```

## How to interpret the output

- **`no_eligible_issues`**: the tick acquired the lock, ran discovery, found no
  eligible candidate, and stopped. No issue was ingested and no executor ran.
  This is the normal idle result and is expected when the backlog is empty.
- **`execution_completed`**: the tick selected one issue, ingested it, and ran
  the executor through the configured runner. The result is left for human
  review; `publication_config.publish_after_execution=false` means no branch
  push or draft PR happened automatically.
- **`blocked`**: a task in the local mirror is in the `blocked` status. Check
  `recent_blocked` for the task key, title, and `blocked_reason`. A closed
  GitHub issue, for example, is mirrored as blocked and is not runnable.
- **ingestion failure count**: how many issues failed pre-task ingestion and are
  recorded in the registry. `quarantined_ingestion_failure_count` is the subset
  that exceeded the retry threshold and is being skipped until cleared.
- **lock contention** (`lock_contention_count`, or last tick `status=locked`):
  another run already held the shared non-overlap lock, so this tick returned a
  safe no-op. Occasional contention is normal timer overlap, not a failure.
- **`waiting_approval` count**: how many tasks have reached `waiting_approval`
  and are waiting for a human reviewer. These are the tasks to review next.

## How to inspect the latest task manually

Use the `selected_task_key` from the last tick (when present) with the existing
read-only waiting-approval summary:

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_waiting_approval.py \
  --task-key AT-GH-123 \
  --db-path /home/ubuntu/.agent-taskflow/state.db
```

You can also read the raw tick directly, for example the last line of the log:

```bash
tail -n 1 /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl \
  | python3 -m json.tool
```

## How to pause cron if needed

Pausing or stopping the schedule is an explicit human/operator action that is
outside this tool. If you need to pause the real scheduled tick, edit the
crontab yourself (for example `crontab -e` and comment out the entry, or stop
the relevant systemd timer with `systemctl --user stop <timer>`). This command
never edits crontab or timers; it only reads logs and local state.

## Read-only guarantee

This tool is observability only. Every run reports an explicit `safety` block:

```text
read_only: true
cron_modified: false
db_written: false
github_called: false
executor_started: false
validator_started: false
issue_ingested: false
branch_pushed: false
draft_pr_created: false
merged: false
approved: false
cleanup_performed: false
branch_deleted: false
worktree_deleted: false
daemon_started: false
scheduler_loop_started: false
```
