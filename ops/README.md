# ops/ — operator scripts

Versioned copies of the operator scripts that run beside agent-taskflow on the
VPS. These are **operator tooling**, not part of the control plane. They never
approve tasks and never merge — human review remains the final gate.

The Ticket path itself needs no operator script: the execution tick and the
integration tick run from cron against the deployed release (see
`deploy/cron/v1-*.cron.example`).

The live working copies run from `~/agent-taskflow-ops/`. This directory is the
version-controlled source of truth for them; changes should land here first.

## Scripts

| Script | Purpose |
| --- | --- |
| `run_notify.sh` | Wrapper that loads config/secrets and runs the notifier. |
| `taskflow_dc_notify.py` | Stdlib-only, read-only notifier: reads the taskflow SQLite mirror, detects tasks that entered a notify-worthy status since the last run, and posts one Discord message per transition with the PR link when available. Its cursor lives in `TASKFLOW_NOTIFY_STATE`, never in the taskflow DB. |

## Secrets

The Discord webhook URL must never enter this repository.

`run_notify.sh` reads `DISCORD_WEBHOOK_URL` from the environment, and falls back
to an untracked `ops/notify.env` only when the variable is unset:

```bash
cp ops/notify.env.example ops/notify.env
chmod 600 ops/notify.env
# edit ops/notify.env, then:
ops/run_notify.sh
```

`ops/notify.env` is listed in `.gitignore`. Only `ops/notify.env.example`, which
carries placeholders, is committed.

## Paths

`run_notify.sh` derives the repo root from its own location (`ops/..`), so a
fresh checkout works without editing. Machine-level paths keep VPS-layout
defaults and are overridable by environment variable:

- `TASKFLOW_DB_PATH` — default `$HOME/.agent-taskflow/state/github_issue_scheduler.sqlite3`
- `TASKFLOW_NOTIFY_STATE` — default `$HOME/agent-taskflow-ops/notify_state.json`
