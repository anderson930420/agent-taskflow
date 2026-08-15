# Operator Notify + Tick + Advisory Recovery Loop

This is the current operator procedure as of 2026-08-15. It monitors the
scheduler, deliberately runs no more than one implementation task, and repairs
the known first-attempt advisory-evidence block without weakening any gate.
`waiting_approval` is a human-review handoff, never approval.

## 1. Notify every five minutes

Schedule `agent-taskflow-ops/run_notify.sh` from cron every five minutes (for
example, `*/5 * * * *`). It opens the scheduler SQLite DB read-only, so it
cannot change task state. Its separate notification-state file only prevents
duplicate messages for the same status transition.

The notifier sends Discord messages for these attention states:

- `waiting_approval`
- `waiting_for_review`
- `blocked`

It also sends low-key terminal confirmations for `accepted`, `completed`, and
`rejected`. Keep the Discord webhook in operator-managed configuration; do not
put it in this repository or a cron entry.

Use the notifier before and after an operator action to make the transition
visible:

```bash
cd /home/ubuntu/agent-taskflow-ops
./run_notify.sh
# Run a manual tick only when the operator has chosen an eligible task.
./run_tick_manual.sh
./run_notify.sh
```

The scheduled tick remains a dry-run watchdog. Do not replace it with this
confirmed manual helper or add `--confirmed` to its cron invocation.

## 2. Run one confirmed tick deliberately

`agent-taskflow-ops/run_tick_manual.sh` wraps
`scripts/run_github_issue_one_task_scheduler_tick.py` with `--confirmed`,
`--executor pi`, and the configured Codex-as-Pi shim through `--pi-bin`. It
also selects the `agent-taskflow-ready` work and runs the `pytest` validator.
It acquires the shared non-overlap lock, processes at most one eligible GitHub
Issue and one task, then exits.

If the JSON result is `locked`, another write-capable run owns the lock. Treat
it as the safe no-op it is; wait for that run rather than retrying in a loop.
The manual tick is execution-only by default: it does not publish a branch or
create a draft PR.

## 3. Recover a first-attempt advisory-evidence block

A successful implementation and passing `pytest` can still first land in
`blocked` at `codex_advisory_evidence`, because the required Codex advisory
artifact is produced only after that attempt has evidence. Work against the
same existing Attempt directory; do not reset the task to create a new attempt.

Set the values from the blocked task's recorded evidence, then create advisory
evidence with the configured headless Codex command:

```bash
cd /home/ubuntu/agent-taskflow
TASK_KEY=AT-GH-123
DB_PATH=/home/ubuntu/.agent-taskflow/state/github_issue_scheduler.sqlite3
ATTEMPT_DIR=/absolute/path/to/the/existing/attempt
WORKTREE_PATH=/absolute/path/to/the/task/worktree
CODEX_HEADLESS_COMMAND='/home/ubuntu/tools/pi-agent/bin/codex exec --sandbox danger-full-access'

PYTHONPATH=. .venv/bin/python3 scripts/run_codex_advisory_review.py \
  --task-key "$TASK_KEY" \
  --repo-path "$PWD" \
  --worktree-path "$WORKTREE_PATH" \
  --artifact-dir "$ATTEMPT_DIR" \
  --confirm-run \
  --codex-command "$CODEX_HEADLESS_COMMAND" \
  --timeout-seconds 600
```

`--confirm-run` invokes Codex once and writes the advisory artifacts in that
Attempt directory. It is advisory-only; it does not validate, approve, merge,
push, clean up, or change task state. Check its generated
`codex-advisory-review.json` and the existing executor and `pytest` evidence.
The gate contract is defined in
[Codex Advisory Reviewer Contract](codex-advisory-review.md).

First run the audited retry as a read-only precondition report. It requires the
task still be `blocked`, executor evidence, passing recorded `pytest` evidence,
and an advisory artifact accepted by the same evidence gate:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/retry_advisory_evidence_transition.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --artifact-dir "$ATTEMPT_DIR" \
  --operator '<stable-operator-id>'
```

Only when that report says all preconditions are satisfied, repeat it with
`--confirm-transition`:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/retry_advisory_evidence_transition.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --artifact-dir "$ATTEMPT_DIR" \
  --operator '<stable-operator-id>' \
  --confirm-transition
```

This is the audited `blocked` to `waiting_approval` recovery transition, not
approval. If any precondition fails, preserve the evidence and investigate the
reported cause instead of repeatedly ticking or retrying.

## 4. PR links and human-only closeout

A Discord PR link is evidence, not an action by the notifier. It is read from
`<artifact-root>/draft_pr/<task-key>/draft_pr.json` after the separate explicit
draft-PR publication workflow records `pr_url` (or its compatible `url` field).
No artifact means no link. Because the manual tick is execution-only, it does
not create that artifact or link.

Humans alone review the implementation, validator output, advisory evidence,
and any draft PR; decide whether to approve or reject; merge when appropriate;
and choose and perform closeout under the separate governed process. This loop
never approves, merges, pushes, closes an issue, or performs cleanup.
